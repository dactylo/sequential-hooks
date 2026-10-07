# pyright: reportPrivateUsage=false

"""Contain child process trees in Windows Job Objects."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast

from sequential_hooks._cleanup import CleanupState
from sequential_hooks._containment import (
    CompletionProcess,
    ContainmentBackend,
    ContainmentError,
    ContainmentReleaseError,
)
from sequential_hooks._containment._uncontained import UncontainedBackend
from sequential_hooks._containment._windows_api import NativeWindowsApi, WindowsApi
from sequential_hooks._types import ContainmentState, FailureKind, StepFailure

if TYPE_CHECKING:
    from sequential_hooks._containment import ManagedStep, StderrReader
    from sequential_hooks._types import Step

_ABORT_GRACE_SECONDS = 1.0
_ARGV_LIMIT_BYTES = 1024 * 1024
_CREATE_NEW_PROCESS_GROUP = cast(
    'int',
    getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200),
)
_FORCE_KILL_WAIT_SECONDS = 1.0
_LAUNCHER_INVARIANT = 3
_LAUNCHER_NOT_FOUND = 1
_LAUNCHER_PATH = Path(__file__).with_name('_windows_launcher.py').resolve()
_LAUNCHER_SPAWN = 2
_LAUNCHER_STATUS_HEADER_BYTES = 5
_LAUNCHER_STATUS_LIMIT_BYTES = 4096
_LAUNCHER_SUCCESS = 0
_RELEASE_ABORT_RESERVE_SECONDS = 1.0
_RELEASE_DEADLINE_SECONDS = 2.0
_PopenFactory = Callable[..., subprocess.Popen[bytes]]
_StartupInfoFactory = Callable[[tuple[int, int]], object]


class _SetHandleInheritable(Protocol):
    """Set whether a native handle is inherited by a child process."""

    def __call__(self, handle: int, *, inheritable: bool) -> None: ...


@dataclass
class _ControlWriteResult:
    """Carry one detached control writer's bounded result."""

    _bytes_written: int = 0
    _error: OSError | None = None


class _LauncherProtocolError(RuntimeError):
    """Report a corrupt or invariant-failure launcher status frame."""


@dataclass(frozen=True)
class _ManagedResources:
    """Carry one launcher's parent-owned descriptors and Job Object."""

    _control_writer: int
    _job_handle: int
    _status_reader: int
    _write_fd: Callable[[int, bytes], int]


def _create_gate_pipes() -> tuple[int, int, int, int]:
    """Create both anonymous pipes without leaking a partial first pair."""
    control_reader, control_writer = os.pipe()
    try:
        status_reader, status_writer = os.pipe()
    except OSError:
        os.close(control_reader)
        os.close(control_writer)
        raise
    return control_reader, control_writer, status_reader, status_writer


def _decode_status_frame(frame: bytes) -> tuple[int, str]:
    """Validate and decode one complete bounded launcher status frame."""
    if len(frame) < _LAUNCHER_STATUS_HEADER_BYTES:
        raise _LauncherProtocolError('launcher status frame is missing or truncated')
    status = frame[0]
    message_length = int.from_bytes(frame[1:_LAUNCHER_STATUS_HEADER_BYTES], 'little')
    if message_length > _LAUNCHER_STATUS_LIMIT_BYTES - _LAUNCHER_STATUS_HEADER_BYTES:
        raise _LauncherProtocolError('launcher status frame exceeds its bound')
    if len(frame) != message_length + _LAUNCHER_STATUS_HEADER_BYTES:
        raise _LauncherProtocolError('launcher status frame is truncated or has trailing bytes')
    try:
        message = frame[_LAUNCHER_STATUS_HEADER_BYTES:].decode('utf-8')
    except UnicodeDecodeError as error:
        raise _LauncherProtocolError('launcher status text is not UTF-8') from error
    return status, message


def _deliver_control_frame(
    ownership_transferred: threading.Event,
    write_fd: Callable[[int, bytes], int],
    descriptor: int,
    frame: bytes,
    result: _ControlWriteResult,
) -> None:
    """Own and deliver one control frame from a detached daemon."""
    ownership_transferred.wait()
    try:
        while result._bytes_written < len(frame):
            try:
                written = write_fd(descriptor, frame[result._bytes_written :])
            except OSError as error:
                result._error = error
                break
            if written <= 0:
                result._error = OSError('launcher control write made no progress')
                break
            result._bytes_written += written
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if result._error is None:
                result._error = error


def _native_handle(fd: int) -> int:
    """Convert one C-runtime descriptor into its Windows kernel handle."""
    msvcrt = __import__('msvcrt')
    return int(msvcrt.get_osfhandle(fd))


def _set_handle_inheritable(handle: int, *, inheritable: bool) -> None:
    """Set one Windows kernel handle's temporary inheritance state."""
    setter = cast('Callable[[int, bool], None]', vars(os)['set_handle_inheritable'])
    setter(handle, inheritable)


def _startup_info(handles: tuple[int, int]) -> object:
    """Build one restricted Windows inherited-handle list."""
    startup_info_type = vars(subprocess)['STARTUPINFO']
    startup_info = startup_info_type()
    startup_info.lpAttributeList = {'handle_list': list(handles)}
    return cast('object', startup_info)


@dataclass(frozen=True)
class _WindowsRuntime:
    """Inject process and descriptor operations for platform-neutral tests."""

    _fd_to_handle: Callable[[int], int] = _native_handle
    _popen_factory: _PopenFactory = subprocess.Popen
    _set_handle_inheritable: _SetHandleInheritable = _set_handle_inheritable
    _startup_info_factory: _StartupInfoFactory = _startup_info
    _write_fd: Callable[[int, bytes], int] = os.write


class _LauncherProcess:
    """Decode launcher status while presenting its child result to the executor."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        status_reader: int,
        step: Step,
    ) -> None:
        """Bind launcher status to the child process result."""
        self._decoded_failure: StepFailure | None = None
        self._ignore_status = False
        self._process = process
        self._result_returncode: int | None = None
        self._status_reader: int | None = status_reader
        self._status_resolved = False
        self._step = step

    def _close_status_reader(self) -> None:
        """Close the parent status descriptor exactly once."""
        if self._status_reader is not None:
            descriptor = self._status_reader
            self._status_reader = None
            os.close(descriptor)

    @property
    def _completion_failure(self) -> StepFailure | None:
        """Return the decoded real-step spawn failure, if any."""
        return self._decoded_failure

    def _discard_status(self) -> None:
        """Stop interpreting status after wrapper-owned termination."""
        self._ignore_status = True
        self._close_status_reader()

    def _read_status_frame(self) -> bytes:
        """Read one bounded status frame through end of file."""
        if self._status_reader is None:
            raise _LauncherProtocolError('launcher status descriptor is unavailable')
        frame = bytearray()
        while chunk := os.read(
            self._status_reader,
            _LAUNCHER_STATUS_LIMIT_BYTES + 1 - len(frame),
        ):
            frame.extend(chunk)
            if len(frame) > _LAUNCHER_STATUS_LIMIT_BYTES:
                break
        return bytes(frame)

    def _resolve_status(self) -> None:
        """Decode one complete launcher status exactly once."""
        if self._status_resolved:
            return
        self._status_resolved = True
        if self._ignore_status:
            raw_returncode = self._process.returncode
            self._result_returncode = (
                None if raw_returncode is None else raw_returncode & 0xFFFFFFFF
            )
            self._close_status_reader()
            return
        try:
            frame = self._read_status_frame()
        finally:
            self._close_status_reader()
        status, message = _decode_status_frame(frame)
        if status == _LAUNCHER_SUCCESS:
            if message:
                raise _LauncherProtocolError('successful launcher status contains diagnostic text')
            if self._process.returncode is None:
                raise _LauncherProtocolError('launcher completed without an exit value')
            self._result_returncode = self._process.returncode & 0xFFFFFFFF
            return
        if not message:
            raise _LauncherProtocolError('failed launcher status omits diagnostic text')
        if status == _LAUNCHER_NOT_FOUND:
            self._decoded_failure = StepFailure(
                FailureKind.NOT_FOUND,
                f'step {self._step.index} ({self._step.name}) not found',
            )
            return
        if status == _LAUNCHER_SPAWN:
            self._decoded_failure = StepFailure(
                FailureKind.SPAWN,
                f'step {self._step.index} ({self._step.name}) could not be spawned',
            )
            return
        if status == _LAUNCHER_INVARIANT:
            raise _LauncherProtocolError(f'launcher invariant failure: {message}')
        raise _LauncherProtocolError(f'unknown launcher status {status}')

    def _raise_release_invariant_if_available(self, deadline: float) -> None:
        """Raise an available status invariant after a partial write."""
        try:
            self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return
        self._resolve_status()
        raise _LauncherProtocolError('launcher completed after a failed partial control frame')

    def poll(self) -> int | None:
        """Poll the launcher and decode status after it exits.

        Returns:
            Exact real-step status, `0` for a decoded spawn failure, or `None`
            while the launcher is running.

        Raises:
            OSError: If status-channel I/O fails.
            _LauncherProtocolError: If status is malformed or violates a
                protocol invariant.
        """
        if self._process.poll() is None:
            return None
        self._resolve_status()
        return 0 if self._decoded_failure is not None else self._result_returncode

    @property
    def returncode(self) -> int | None:
        """Return the exact real-step status after status resolution.

        Returns:
            Exact real-step status, or `None` before status resolution or when
            launcher status reports a launch failure.
        """
        if self._status_resolved:
            return self._result_returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the launcher and return the exact real-step status.

        Args:
            timeout: Maximum seconds to wait, or `None` to wait indefinitely.

        Returns:
            Exact real-step status, or `0` when `_completion_failure` carries a
            decoded real-step launch failure.

        Raises:
            OSError: If status-channel I/O fails.
            subprocess.TimeoutExpired: If the launcher remains running after
                `timeout`.
            _LauncherProtocolError: If status is malformed or violates a
                protocol invariant.
        """
        self._process.wait(timeout=timeout)
        self._resolve_status()
        return 0 if self._result_returncode is None else self._result_returncode


class _WindowsManagedStep:
    """Own one launcher gate, optional Job Object, and parent status handle."""

    def __init__(
        self,
        api: WindowsApi,
        process: subprocess.Popen[bytes],
        step: Step,
        resources: _ManagedResources,
    ) -> None:
        """Take ownership of one gated Windows child and its resources."""
        self._api = api
        self._assigned = False
        self._containment = ContainmentState.CONTAINED
        self._control_writer: int | None = resources._control_writer
        self._job_cleanup_deadline: float | None = None
        self._job_handle: int | None = resources._job_handle
        self._launcher_process = _LauncherProcess(process, resources._status_reader, step)
        self._process = process
        self._released = False
        self._step = step
        self._write_fd = resources._write_fd

    def _close_control_writer(self) -> None:
        """Close the parent control writer exactly once."""
        if self._control_writer is not None:
            descriptor = self._control_writer
            self._control_writer = None
            os.close(descriptor)

    def _close_job(self) -> None:
        """Close the wrapper-owned Job Object handle exactly once."""
        if self._job_handle is not None:
            handle = self._job_handle
            self._job_handle = None
            self._api.close_handle(handle)

    def _close_process_pipes(self, cleanup: CleanupState) -> None:
        """Attempt each wrapper-owned standard-pipe close."""
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                cleanup.attempt(stream.close)

    def _mark_assigned(self) -> None:
        """Record successful assignment before the launcher can be released."""
        self._assigned = True

    @property
    def _raw_process(self) -> subprocess.Popen[bytes]:
        """Return the blocked launcher for pre-release assignment."""
        return self._process

    def _release_job(self) -> None:
        """Drop a refused Job Object under the explicit uncontained override."""
        self._containment = ContainmentState.UNCONTAINED
        self._close_job()

    def _stop_unassigned_launcher(self, grace_seconds: float) -> None:
        """Bound cleanup of a launcher that is not protected by the job."""
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=_FORCE_KILL_WAIT_SECONDS)

    def _terminate_job_tree(self) -> None:
        """Terminate and confirm all job exits within one retained deadline."""
        job_handle = self._job_handle
        if job_handle is None:
            return
        if self._job_cleanup_deadline is None:
            self._job_cleanup_deadline = time.monotonic() + _FORCE_KILL_WAIT_SECONDS
            self._api.terminate_job(job_handle)
        remaining = max(0.0, self._job_cleanup_deadline - time.monotonic())
        if not self._api.wait_job(job_handle, remaining):
            raise ContainmentError(
                'Windows job completion could not be confirmed after termination'
            )
        self._process.wait(timeout=max(0.0, self._job_cleanup_deadline - time.monotonic()))

    def _wait_for_cleanup(
        self,
        cleanup: CleanupState,
        deadline: float,
        limit: float,
    ) -> bool:
        """Wait for cleanup completion while retaining ordinary failures."""
        try:
            self._process.wait(timeout=min(limit, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            return False
        except (OSError, RuntimeError) as error:
            cleanup.record(error)
            return False
        return True

    def _terminate_before_release(self, cleanup: CleanupState, deadline: float) -> None:
        """Terminate and reap the blocked launcher within one deadline."""
        terminated = False
        job_handle = self._job_handle
        if self._assigned and job_handle is not None:
            terminated = cleanup.attempt(lambda: self._api.terminate_job(job_handle))
        if not terminated:
            cleanup.attempt(self._process.terminate)
        if self._wait_for_cleanup(cleanup, deadline, _FORCE_KILL_WAIT_SECONDS):
            return
        cleanup.attempt(self._process.kill)
        if not self._wait_for_cleanup(cleanup, deadline, _FORCE_KILL_WAIT_SECONDS):
            cleanup.record(
                _LauncherProtocolError('launcher could not be reaped after termination')
            )

    def _dispose_before_release(
        self,
        first_error: Exception | None = None,
        *,
        deadline: float | None = None,
        force_terminate: bool = False,
    ) -> Exception | None:
        """Dispose containment owners without touching handed-off pipes."""
        cleanup = CleanupState(first_error)
        end = (
            time.monotonic() + _ABORT_GRACE_SECONDS + _FORCE_KILL_WAIT_SECONDS
            if deadline is None
            else deadline
        )
        cleanup.attempt(self._close_control_writer)

        exited = not force_terminate and self._wait_for_cleanup(
            cleanup,
            end,
            _ABORT_GRACE_SECONDS,
        )
        if not exited:
            self._terminate_before_release(cleanup, end)

        cleanup.attempt(self._launcher_process._discard_status)
        cleanup.attempt(self._close_job)
        return cleanup.first_error

    def _dispose_setup_failure(
        self,
        first_error: Exception | None = None,
        *,
        force_terminate: bool = False,
    ) -> Exception | None:
        """Dispose every owner before standard pipes reach the executor."""
        cleanup = CleanupState(
            self._dispose_before_release(
                first_error,
                force_terminate=force_terminate,
            )
        )
        self._close_process_pipes(cleanup)
        return cleanup.first_error

    def abort_before_release(self) -> None:
        """Dispose of the blocked launcher without starting the real step.

        Raises:
            Exception: If launcher or Job Object cleanup fails.
        """
        if error := self._dispose_before_release():
            raise error

    @property
    def completion_failure(self) -> StepFailure | None:
        """Return a decoded launcher-reported real-step spawn failure.

        Returns:
            Decoded spawn failure, or `None` when none is available.
        """
        return self._launcher_process._completion_failure

    @property
    def containment(self) -> ContainmentState:
        """Return whether this launcher remains assigned to its Job Object.

        Returns:
            Current Job Object containment state.
        """
        return self._containment

    def finish(self) -> None:
        """Confirm remaining job members have exited, then close the handle.

        Raises:
            Exception: If termination, completion, or handle cleanup fails.
                The first failure is preserved while handle closure is tried.
        """
        if self._job_handle is not None:
            cleanup = CleanupState()
            try:
                with cleanup:
                    self._terminate_job_tree()
            finally:
                cleanup.attempt(self._close_job)
            if cleanup.first_error is not None:
                raise cleanup.first_error
        elif self._process.poll() is None:
            self._stop_unassigned_launcher(_ABORT_GRACE_SECONDS)

    def forward_signal(self, signum: int) -> None:
        """Try Ctrl+Break for the launcher's console process group.

        Args:
            signum: Wrapper signal, whose Windows console form is Ctrl+Break.
        """
        del signum
        if self._process.poll() is None:
            self._api.generate_ctrl_break(self._process.pid)

    @property
    def process(self) -> CompletionProcess:
        """Return a completion facade exposing the exact real-step result.

        Returns:
            Launcher facade used to observe real-step completion.
        """
        return self._launcher_process

    def release(self) -> None:
        """Release after successful assignment or explicitly authorized uncontained fallback.

        Raises:
            _LauncherProtocolError: If the gate state, control frame, launcher
                status, writer startup, or cleanup violates the protocol.
            ContainmentReleaseError: If control-frame delivery times out or
                fails before the launcher accepts it.
        """
        if self._released or self._control_writer is None:
            raise _LauncherProtocolError('launcher control gate was already released')
        argv_bytes = json.dumps(
            self._step.argv,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode()
        if len(argv_bytes) > _ARGV_LIMIT_BYTES:
            raise _LauncherProtocolError('launcher argv frame exceeds its bound')
        frame = b'R' + len(argv_bytes).to_bytes(8, 'little') + argv_bytes
        descriptor = self._control_writer
        ownership_transferred = threading.Event()
        result = _ControlWriteResult()
        deadline = time.monotonic() + _RELEASE_DEADLINE_SECONDS
        try:
            writer = threading.Thread(
                target=_deliver_control_frame,
                args=(
                    ownership_transferred,
                    self._write_fd,
                    descriptor,
                    frame,
                    result,
                ),
                name=f'sequential-hooks-control-writer-{self._step.index}',
                daemon=True,
            )
            writer.start()
        except RuntimeError as error:
            raise _LauncherProtocolError('launcher control writer could not be started') from error
        self._control_writer = None
        ownership_transferred.set()
        writer.join(
            max(
                0.0,
                deadline - _RELEASE_ABORT_RESERVE_SECONDS - time.monotonic(),
            )
        )
        if writer.is_alive():
            release_error = ContainmentReleaseError('launcher control frame delivery timed out')
            cleanup_error = self._dispose_before_release(
                deadline=deadline,
                force_terminate=True,
            )
            writer.join(max(0.0, deadline - time.monotonic()))
            if cleanup_error is not None:
                if isinstance(cleanup_error, _LauncherProtocolError):
                    raise cleanup_error
                raise _LauncherProtocolError(
                    'launcher cleanup failed after control delivery timeout'
                ) from cleanup_error
            raise release_error
        if result._error is not None:
            if result._bytes_written:
                self._launcher_process._raise_release_invariant_if_available(deadline)
            raise ContainmentReleaseError(
                'launcher control frame could not be written'
            ) from result._error
        self._released = True

    @property
    def stderr(self) -> StderrReader:
        """Return the launcher's inherited real-step stderr pipe.

        Returns:
            Binary stderr reader inherited through the launcher.
        """
        return cast('StderrReader', self._process.stderr)

    @property
    def stdin(self) -> BinaryIO:
        """Return the launcher's inherited real-step stdin pipe.

        Returns:
            Binary stdin writer inherited through the launcher.
        """
        return cast('BinaryIO', self._process.stdin)

    @property
    def stdout(self) -> BinaryIO:
        """Return the launcher's inherited real-step stdout pipe.

        Returns:
            Binary stdout reader inherited through the launcher.
        """
        return cast('BinaryIO', self._process.stdout)

    def terminate_tree(self, grace_seconds: float) -> None:
        """Wait for the launcher, then terminate and confirm all job exits.

        Call `forward_signal()` first when a graceful console signal is
        required. Without a retained Job Object, forced termination covers only
        the launcher.

        Args:
            grace_seconds: Initial wait in seconds before forced termination.

        Raises:
            ContainmentError: If job completion cannot be confirmed in time.
            OSError: If native job termination or accounting fails.
        """
        self._launcher_process._discard_status()
        if self._process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=max(0.0, grace_seconds))
        if self._assigned and self._job_handle is not None:
            self._terminate_job_tree()
        elif self._process.poll() is None:
            self._stop_unassigned_launcher(grace_seconds)

    @property
    def uses_windows_exit_codes(self) -> bool:
        """Return whether the completion facade carries unsigned Windows status.

        Returns:
            `True` because the facade exposes unsigned Windows exit codes.
        """
        return True


class WindowsBackend:
    """Create launcher-gated native-Windows managed steps."""

    def __init__(
        self,
        api: WindowsApi | None = None,
        *,
        runtime: _WindowsRuntime | None = None,
        uncontained_backend: ContainmentBackend | None = None,
    ) -> None:
        """Select native Windows operations and explicit fallback behavior.

        Args:
            api: Windows API implementation, or the native implementation.
            runtime: Process and descriptor operations, or their native
                defaults.
            uncontained_backend: Backend used for explicitly allowed fallback.

        Raises:
            OSError: If `api` is omitted and native Windows APIs are
                unavailable.
        """
        self._api = api or NativeWindowsApi()
        self._runtime = runtime or _WindowsRuntime()
        self._uncontained_backend = uncontained_backend or UncontainedBackend()

    def _fallback_or_raise(
        self,
        step: Step,
        *,
        allow_uncontained: bool,
        message: str,
    ) -> ManagedStep:
        """Return an explicit fallback or raise one containment refusal."""
        if not allow_uncontained:
            raise ContainmentError(message)
        return self._uncontained_backend.start(step, allow_uncontained=True)

    def _start_launcher(self, step: Step, job_handle: int) -> _WindowsManagedStep:
        """Start one isolated launcher with only its two gate handles inherited."""
        control_reader, control_writer, status_reader, status_writer = _create_gate_pipes()
        inheritable_handles: list[int] = []
        process: subprocess.Popen[bytes]
        try:
            try:
                control_handle = self._runtime._fd_to_handle(control_reader)
                status_handle = self._runtime._fd_to_handle(status_writer)
                for handle in (control_handle, status_handle):
                    self._runtime._set_handle_inheritable(handle, inheritable=True)
                    inheritable_handles.append(handle)
                command = (
                    sys.executable,
                    '-I',
                    '-S',
                    str(_LAUNCHER_PATH),
                    str(control_handle),
                    str(status_handle),
                )
                process = self._runtime._popen_factory(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                    creationflags=_CREATE_NEW_PROCESS_GROUP,
                    startupinfo=self._runtime._startup_info_factory(
                        (control_handle, status_handle)
                    ),
                )
            finally:
                for handle in inheritable_handles:
                    with contextlib.suppress(OSError):
                        self._runtime._set_handle_inheritable(handle, inheritable=False)
                for descriptor in (control_reader, status_writer):
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
        except Exception:
            for descriptor in (control_writer, status_reader):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        managed = _WindowsManagedStep(
            self._api,
            process,
            step,
            _ManagedResources(
                control_writer,
                job_handle,
                status_reader,
                self._runtime._write_fd,
            ),
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            error = _LauncherProtocolError('launcher standard pipes were not created')
            managed._dispose_setup_failure(error, force_terminate=True)
            raise error
        return managed

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        """Return a direct uncontained fallback after a disposed gate.

        Args:
            step: Step whose control frame could not be delivered.
            failed: Failed launcher already disposed by the executor.
            allow_uncontained: Whether direct fallback is explicitly allowed.

        Returns:
            Fresh direct uncontained process.

        Raises:
            ContainmentError: If direct fallback was not explicitly allowed.
            OSError: If starting the fallback process fails.
        """
        del failed
        return self._fallback_or_raise(
            step,
            allow_uncontained=allow_uncontained,
            message='containment release failed without explicit override',
        )

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        """Create, assign, and retain one blocked Windows launcher.

        Args:
            step: Literal real-step argv.
            allow_uncontained: Whether exact setup failures may use the
                approved reduced-guarantee row.

        Returns:
            Assigned blocked launcher, or an explicitly allowed fallback.

        Raises:
            ContainmentError: If setup failed without an applicable override.
            _LauncherProtocolError: If launcher setup violates its protocol or
                invariants.
            OSError: If setup or cleanup fails outside the override boundary.
        """
        try:
            job_handle = self._api.create_kill_on_close_job()
        except OSError as error:
            return self._fallback_or_raise(
                step,
                allow_uncontained=allow_uncontained,
                message=f'Job Object creation failed: {error}',
            )

        try:
            managed = self._start_launcher(step, job_handle)
        except _LauncherProtocolError:
            raise
        except OSError as error:
            with contextlib.suppress(OSError):
                self._api.close_handle(job_handle)
            return self._fallback_or_raise(
                step,
                allow_uncontained=allow_uncontained,
                message=f'launcher creation failed: {error}',
            )
        except Exception:
            with contextlib.suppress(OSError):
                self._api.close_handle(job_handle)
            raise

        try:
            self._api.assign_pid(job_handle, managed._raw_process.pid)
        except OSError as error:
            if allow_uncontained:
                try:
                    managed._release_job()
                except OSError:
                    managed._dispose_setup_failure(error)
                    raise ContainmentError(f'Job Object assignment failed: {error}') from error
                else:
                    return managed
            managed._dispose_setup_failure(error)
            raise ContainmentError(f'Job Object assignment failed: {error}') from error
        except Exception as error:
            managed._dispose_setup_failure(error)
            raise
        managed._mark_assigned()
        return managed
