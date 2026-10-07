# pyright: reportPrivateUsage=false

"""Execute bounded child processes with platform containment."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sequential_hooks._cleanup import CleanupState
from sequential_hooks._containment import (
    CompletionFailureProvider,
    ContainmentBackend,
    ContainmentError,
    ContainmentReleaseError,
    ManagedStep,
    WindowsExitCodeProvider,
    current_backend,
)
from sequential_hooks._downstream import is_downstream_closure
from sequential_hooks._executable import ExecutableStatus, resolve_executable
from sequential_hooks._types import FailureKind, Step, StepFailure, StepResult

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager
    from types import FrameType
    from typing import BinaryIO, NoReturn

    from sequential_hooks._containment import StderrReader
    from sequential_hooks._diagnostics import Diagnostics, StderrRelay

    type _WorkerStream = BinaryIO | StderrReader
_CANCELLATION_CLOSE_OBSERVE_SECONDS = 0.1
_OUTPUT_WORKER_JOIN_SECONDS = 1.0
_STDOUT_LIMIT_BYTES = 1024 * 1024
_TERMINATION_GRACE_SECONDS = 1.0
_WINDOWS_CRASH_CODES = frozenset({0x80000001, 0x80000002, 0x80000003, 0x80000004})
_WINDOWS_CRASH_RANGE_END = 0xCFFFFFFF
_WINDOWS_CRASH_RANGE_START = 0xC0000000
_WINDOWS_RUNTIME_CRASH_RANGE_END = 0xEFFFFFFF
_WINDOWS_RUNTIME_CRASH_RANGE_START = 0xE0000000


class ForwardedSignal(BaseException):
    """Report a wrapper signal after its active child has been cleaned up.

    The executor raises this boundary exception at most once per signal-
    forwarding context and only after active-child cleanup.

    Attributes:
        signum: Native signal number received by the wrapper.
    """

    def __init__(self, signum: int) -> None:
        """Retain the native signal forwarded after child cleanup.

        Args:
            signum: Native signal number received by the wrapper.
        """
        super().__init__(f'signal {signum} forwarded')
        self.signum = signum


@dataclass(frozen=True)
class _AttemptPipes:
    """Carry one attempt's parent pipe handles."""

    _stderr: StderrReader
    _stdin: BinaryIO
    _stdout: BinaryIO


class _Attempt:
    """Own one managed step's output buffers, relay, and pipe workers."""

    def __init__(
        self,
        relay: StderrRelay,
        pipes: _AttemptPipes,
        input_bytes: bytes,
        thread_prefix: str,
    ) -> None:
        """Create pipe workers for one managed attempt."""
        self._output = bytearray()
        self._output_exceeded = threading.Event()
        self._relay = relay
        self._stderr_worker = _Worker(
            f'{thread_prefix}-stderr',
            pipes._stderr,
            lambda: _read_stderr(pipes._stderr, relay),
        )
        self._stdin_worker = _Worker(
            f'{thread_prefix}-stdin',
            pipes._stdin,
            lambda: _write_stdin(pipes._stdin, input_bytes),
            close_in_worker=True,
        )
        self._stdout_worker = _Worker(
            f'{thread_prefix}-stdout',
            pipes._stdout,
            lambda: _read_stdout(
                pipes._stdout,
                self._output,
                self._output_exceeded,
            ),
        )
        self._workers = (
            self._stdout_worker,
            self._stderr_worker,
            self._stdin_worker,
        )

    def _first_worker_error(self) -> Exception | None:
        """Return the first completed worker failure in lifecycle order."""
        return next(
            (error for worker in self._workers if (error := worker._failure()) is not None),
            None,
        )

    def _start(self) -> None:
        """Start readers first, then the stdin writer."""
        self._stdout_worker._start()
        self._stderr_worker._start()
        self._stdin_worker._start()


@dataclass
class _CancellationClose:
    """Retain one asynchronous pipe-close failure without a traceback."""

    _completed: threading.Event = field(default_factory=threading.Event)
    _error: Exception | None = None


@dataclass
class _Completion:
    """Carry saved process state through post-completion cleanup."""

    _returncode: int | None
    _blocking_stderr: str = ''
    _failure_kind: FailureKind | None = None


@dataclass(frozen=True)
class _Recovery:
    """Request another attempt with one fresh managed process."""

    _managed: ManagedStep


@dataclass(frozen=True)
class _RunRequest:
    """Carry one immutable executor request through recovery."""

    _allow_uncontained: bool
    _input_bytes: bytes
    _launch_step: Step
    _step: Step
    _timeout: float | None


class ProcessExecutor:
    """Execute literal argv with bounded platform containment and pipes.

    Each `run()` call owns a fresh containment backend, process, pipe-worker
    set, and timeout budget. Use `signal_forwarding()` around a command chain
    when wrapper signals must be forwarded to its active child. The context
    restores prior handlers and propagates the first forwarded signal only
    after cleanup. Unexpected containment, pipe, and cleanup failures propagate
    from `run()` unchanged.
    """

    def __init__(self, diagnostics: Diagnostics) -> None:
        """Retain the diagnostic sink for bounded child execution.

        Args:
            diagnostics: Sink for bounded, labeled child diagnostics.
        """
        self._diagnostics = diagnostics
        self._forwarded_signal_raised = False
        self._forwarded_signum: int | None = None
        self._signal_lock = threading.RLock()

    def _build_result(
        self,
        step: Step,
        managed: ManagedStep,
        attempt: _Attempt,
        completion: _Completion,
    ) -> StepResult:
        """Build one bounded result after every cleanup attempt."""
        returncode = completion._returncode
        failure_kind = completion._failure_kind
        failure: StepFailure | None = None
        completion_failure = (
            managed.completion_failure if isinstance(managed, CompletionFailureProvider) else None
        )
        if sys.platform == 'win32':
            uses_windows_exit_codes = True
        else:
            uses_windows_exit_codes = (
                isinstance(managed, WindowsExitCodeProvider) and managed.uses_windows_exit_codes
            )
        if failure_kind is None and completion_failure is not None:
            failure = completion_failure
        elif failure_kind is FailureKind.OUTPUT_LIMIT:
            failure = StepFailure(
                failure_kind,
                f'step {step.index} ({step.name}) exceeded stdout limit',
                returncode,
            )
        elif failure_kind is FailureKind.OUTPUT_WORKER_TIMEOUT:
            failure = StepFailure(
                failure_kind,
                f'step {step.index} ({step.name}) output workers did not stop',
                returncode,
            )
        elif failure_kind is FailureKind.TIMEOUT:
            failure = StepFailure(
                failure_kind,
                f'step {step.index} ({step.name}) timed out',
                returncode,
            )
        elif returncode is not None and uses_windows_exit_codes and _is_windows_crash(returncode):
            failure = StepFailure(
                FailureKind.CRASH,
                f'step {step.index} ({step.name}) crashed with Windows status '
                f'0x{returncode & 0xFFFFFFFF:08X}',
                returncode,
            )
        elif returncode is not None and returncode < 0:
            failure = StepFailure(
                FailureKind.CRASH,
                f'step {step.index} ({step.name}) crashed with signal {-returncode}',
                returncode,
            )
        return StepResult(
            returncode,
            bytes(attempt._output),
            completion._blocking_stderr,
            failure,
            managed.containment,
        )

    def _cleanup_attempt(
        self,
        attempt: _Attempt,
        action: Callable[[], None],
        first_error: Exception | None,
    ) -> tuple[str, bool, Exception | None]:
        """Dispose containment, bound joins, and close every parent pipe."""
        cleanup = CleanupState(first_error)
        cleanup.attempt(action)

        deadline = time.monotonic() + _OUTPUT_WORKER_JOIN_SECONDS
        for worker in attempt._workers:
            worker._join(max(0.0, deadline - time.monotonic()))

        worker_timed_out = any(worker._is_alive() for worker in attempt._workers)
        for worker in attempt._workers:
            if worker._is_alive():
                cleanup.attempt(worker._cancel_pipe)
            else:
                cleanup.attempt(worker._close_pipe)

        cancellation_deadline = time.monotonic() + _CANCELLATION_CLOSE_OBSERVE_SECONDS
        for worker in attempt._workers:
            cleanup.record(worker._failure())

        for worker in attempt._workers:
            worker._observe_cancellation(max(0.0, cancellation_deadline - time.monotonic()))
        for worker in attempt._workers:
            cleanup.record(worker._failure())
        blocking_stderr = ''
        with cleanup:
            blocking_stderr = attempt._relay.close()
        return blocking_stderr, worker_timed_out, cleanup.first_error

    def _abort_release_exception(
        self,
        managed: ManagedStep,
        attempt: _Attempt,
        release_error: Exception,
    ) -> NoReturn:
        """Abort a failed release, clean every pipe, and re-raise."""
        _, _, error = self._cleanup_attempt(
            attempt,
            managed.abort_before_release,
            release_error,
        )
        if error is not None:
            raise error
        raise RuntimeError('release failed without an exception')

    def _containment_failure(self, step: Step, error: ContainmentError) -> StepResult:
        """Map a containment refusal to one bounded step result."""
        return StepResult(
            returncode=None,
            stdout=b'',
            blocking_stderr='',
            failure=StepFailure(
                FailureKind.CONTAINMENT,
                f'step {step.index} ({step.name}) containment failed: {error}',
            ),
        )

    def _create_attempt(self, managed: ManagedStep, request: _RunRequest) -> _Attempt:
        """Create fresh output state and workers for one managed process."""
        acquired_pipes: list[_WorkerStream] = []
        relay: StderrRelay | None = None
        attempt: _Attempt | None = None
        setup = CleanupState()
        with setup:
            stdin = managed.stdin
            acquired_pipes.append(stdin)
            stdout = managed.stdout
            acquired_pipes.append(stdout)
            stderr = managed.stderr
            acquired_pipes.append(stderr)
            relay = self._diagnostics.relay(request._step)
            attempt = _Attempt(
                relay,
                _AttemptPipes(stderr, stdin, stdout),
                request._input_bytes,
                f'sequential-hooks-step-{request._step.index}',
            )
        if attempt is not None:
            return attempt

        cleanup = CleanupState(setup.first_error)
        cleanup.attempt(managed.abort_before_release)
        for stream in acquired_pipes:
            cleanup.attempt(stream.close)
        if relay is not None:
            cleanup.attempt(relay.close)
        if cleanup.first_error is not None:
            raise cleanup.first_error
        raise RuntimeError('attempt construction failed without an exception')

    def _pending_signal(self) -> int | None:
        """Return the first signal received in the forwarding context."""
        with self._signal_lock:
            return self._forwarded_signum

    def _monitor_step(
        self,
        managed: ManagedStep,
        attempt: _Attempt,
        deadline: float | None,
    ) -> tuple[FailureKind | None, Exception | None]:
        """Observe immediate completion while enforcing every bounded stop."""
        failure_kind: FailureKind | None = None
        worker_error: Exception | None = None
        while True:
            forwarded_signum = self._pending_signal()
            if forwarded_signum is not None:
                managed.forward_signal(forwarded_signum)
                managed.terminate_tree(_TERMINATION_GRACE_SECONDS)
                break

            if attempt._output_exceeded.is_set():
                failure_kind = FailureKind.OUTPUT_LIMIT
                managed.terminate_tree(_TERMINATION_GRACE_SECONDS)
                break

            worker_error = attempt._first_worker_error()
            if worker_error is not None:
                managed.terminate_tree(_TERMINATION_GRACE_SECONDS)
                break

            if managed.process.poll() is not None:
                break

            if deadline is not None and time.monotonic() >= deadline:
                failure_kind = FailureKind.TIMEOUT
                managed.terminate_tree(_TERMINATION_GRACE_SECONDS)
                break

            wait_time = 0.01
            if deadline is not None:
                wait_time = min(wait_time, max(0.0, deadline - time.monotonic()))
            with contextlib.suppress(subprocess.TimeoutExpired):
                managed.process.wait(timeout=wait_time)
        return failure_kind, worker_error

    def _raise_pending_signal(self) -> None:
        """Raise an intercepted signal at most once per forwarding context."""
        with self._signal_lock:
            if self._forwarded_signum is None or self._forwarded_signal_raised:
                return
            self._forwarded_signal_raised = True
            forwarded_signum = self._forwarded_signum
        raise ForwardedSignal(forwarded_signum)

    def _complete_attempt(
        self,
        managed: ManagedStep,
        attempt: _Attempt,
        request: _RunRequest,
        deadline: float | None,
    ) -> StepResult:
        """Observe and clean one successfully released managed process."""
        monitor_error: Exception | None = None
        failure_kind: FailureKind | None = None
        worker_error: Exception | None = None
        monitor_cleanup = CleanupState()
        with monitor_cleanup:
            failure_kind, worker_error = self._monitor_step(
                managed,
                attempt,
                deadline,
            )
        monitor_error = monitor_cleanup.first_error

        completion = _Completion(
            managed.process.returncode,
            _failure_kind=failure_kind,
        )
        first_error = worker_error if worker_error is not None else monitor_error
        blocking_stderr, worker_timed_out, error = self._cleanup_attempt(
            attempt,
            managed.finish,
            first_error,
        )
        completion._blocking_stderr = blocking_stderr
        if error is not None:
            raise error
        self._raise_pending_signal()
        if worker_timed_out:
            completion._failure_kind = FailureKind.OUTPUT_WORKER_TIMEOUT
        elif completion._failure_kind is None and attempt._output_exceeded.is_set():
            completion._failure_kind = FailureKind.OUTPUT_LIMIT
        return self._build_result(request._step, managed, attempt, completion)

    def _receive_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Record the first wrapper signal for main-thread forwarding."""
        with self._signal_lock:
            if self._forwarded_signum is None:
                self._forwarded_signum = signum

    @contextlib.contextmanager
    def _signal_forwarding(self) -> Generator[None]:
        """Install and restore signal handlers around one executor context."""
        signal_names = ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK')
        handled_signals = tuple(
            signum for name in signal_names if (signum := getattr(signal, name, None)) is not None
        )
        previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}
        with self._signal_lock:
            self._forwarded_signal_raised = False
            self._forwarded_signum = None
        try:
            for signum in handled_signals:
                signal.signal(signum, self._receive_signal)
            yield
        finally:
            restoration = CleanupState()
            for signum, handler in previous_handlers.items():
                if handler is None:
                    continue
                restoration.attempt(lambda s=signum, h=handler: signal.signal(s, h))
            self._raise_pending_signal()
            if restoration.first_error is not None:
                raise restoration.first_error

    def _spawn_failure(self, step: Step, error: OSError) -> StepResult:
        """Map an operating-system spawn error to a bounded result."""
        kind = FailureKind.NOT_FOUND if isinstance(error, FileNotFoundError) else FailureKind.SPAWN
        category = 'not found' if kind is FailureKind.NOT_FOUND else 'could not be spawned'
        return StepResult(
            returncode=None,
            stdout=b'',
            blocking_stderr='',
            failure=StepFailure(kind, f'step {step.index} ({step.name}) {category}'),
        )

    def _recover_release_failure(
        self,
        backend: ContainmentBackend,
        managed: ManagedStep,
        attempt: _Attempt,
        request: _RunRequest,
    ) -> _Recovery | StepResult:
        """Dispose a failed gate before selecting an allowed fallback."""
        blocking_stderr, worker_timed_out, error = self._cleanup_attempt(
            attempt,
            managed.abort_before_release,
            None,
        )
        if error is not None:
            raise error
        if worker_timed_out:
            completion = _Completion(
                managed.process.returncode,
                blocking_stderr,
                FailureKind.OUTPUT_WORKER_TIMEOUT,
            )
            return self._build_result(request._step, managed, attempt, completion)

        self._raise_pending_signal()
        try:
            recovered = backend.recover_release_failure(
                request._launch_step,
                managed,
                allow_uncontained=request._allow_uncontained,
            )
        except ContainmentError as error:
            return self._containment_failure(request._step, error)
        except OSError as error:
            return self._spawn_failure(request._step, error)
        if recovered is managed:
            return self._containment_failure(
                request._step,
                ContainmentError('release recovery reused the failed managed process'),
            )
        return _Recovery(recovered)

    def _run_attempt(
        self,
        backend: ContainmentBackend,
        managed: ManagedStep,
        request: _RunRequest,
        deadline: float | None,
    ) -> _Recovery | StepResult:
        """Run one managed process through release or recovery."""
        attempt = self._create_attempt(managed, request)
        start_cleanup = CleanupState()
        with start_cleanup:
            attempt._start()
        if start_cleanup.first_error is not None:
            _, _, error = self._cleanup_attempt(
                attempt,
                managed.abort_before_release,
                start_cleanup.first_error,
            )
            if error is not None:
                raise error
            raise RuntimeError('worker startup failed without an exception')
        release_cleanup = CleanupState()
        release_cleanup.attempt(managed.release)
        release_error = release_cleanup.first_error
        if isinstance(release_error, ContainmentReleaseError):
            return self._recover_release_failure(
                backend,
                managed,
                attempt,
                request,
            )
        if release_error is not None:
            return self._abort_release_exception(managed, attempt, release_error)
        return self._complete_attempt(managed, attempt, request, deadline)

    def _run_managed(
        self,
        backend: ContainmentBackend,
        managed: ManagedStep,
        request: _RunRequest,
    ) -> StepResult:
        """Run one managed attempt, including allowed release recovery."""
        deadline = time.monotonic() + request._timeout if request._timeout is not None else None
        while True:
            outcome = self._run_attempt(
                backend,
                managed,
                request,
                deadline,
            )
            if isinstance(outcome, _Recovery):
                managed = outcome._managed
                continue
            return outcome

    def run(
        self,
        step: Step,
        input_bytes: bytes,
        timeout: float | None,
        *,
        allow_uncontained: bool,
    ) -> StepResult:
        """Execute one bounded step through the selected containment backend.

        Args:
            step: Literal child argv and one-based diagnostic index.
            input_bytes: Native hook bytes to deliver through stdin.
            timeout: Optional monotonic timeout in seconds.
            allow_uncontained: Whether explicit uncontained fallback is
                allowed.

        Returns:
            Raw status, bounded outputs, and any infrastructure failure.

        Raises:
            ForwardedSignal: If the wrapper received a handled signal.
            Exception: If an unexpected lifecycle or pipe operation failed.
        """
        self._raise_pending_signal()
        resolution = resolve_executable(
            step.argv[0],
            path_value=os.environ.get('PATH'),
            windows=sys.platform == 'win32',
            pathext_value=os.environ.get('PATHEXT'),
        )
        if resolution.status is ExecutableStatus.NOT_FOUND:
            return StepResult(
                returncode=None,
                stdout=b'',
                blocking_stderr='',
                failure=StepFailure(
                    FailureKind.NOT_FOUND,
                    f'step {step.index} ({step.name}) not found',
                ),
            )
        if resolution.status is ExecutableStatus.UNSUPPORTED_SHIM:
            correction = resolution.correction or 'use a native executable entry point'
            return StepResult(
                returncode=None,
                stdout=b'',
                blocking_stderr='',
                failure=StepFailure(
                    FailureKind.UNSUPPORTED_EXECUTABLE,
                    f'step {step.index} ({step.name}) is unsupported: {correction}',
                ),
            )

        resolved = resolution.resolved
        if resolved is None:
            raise RuntimeError('runnable executable resolution omitted its path')
        launch_path = resolved if resolved.is_absolute() else resolved.absolute()
        launch_step = Step(step.index, (str(launch_path), *step.argv[1:]))

        try:
            backend = current_backend()
            self._raise_pending_signal()
            managed = backend.start(launch_step, allow_uncontained=allow_uncontained)
        except ContainmentError as error:
            return self._containment_failure(step, error)
        except OSError as error:
            return self._spawn_failure(step, error)
        request = _RunRequest(
            _allow_uncontained=allow_uncontained,
            _input_bytes=input_bytes,
            _launch_step=launch_step,
            _step=step,
            _timeout=timeout,
        )
        return self._run_managed(
            backend,
            managed,
            request,
        )

    def signal_forwarding(self) -> AbstractContextManager[None]:
        """Return a context that forwards handled signals to the active child.

        Returns:
            Context manager that restores every previous signal handler.
        """
        return self._signal_forwarding()


class _Worker:
    """Run one daemon pipe operation and retain its first exception."""

    def __init__(
        self,
        name: str,
        stream: _WorkerStream,
        target: Callable[[], None],
        *,
        close_in_worker: bool = False,
    ) -> None:
        """Prepare one daemon pipe operation without starting its thread."""
        self._close_claimed = False
        self._close_in_worker = close_in_worker
        self._close_lock = threading.Lock()
        self._close_result = _CancellationClose()
        self._cancellation_started = False
        self._stream = stream
        self._target_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._target = target

    def _cancel_pipe(self) -> None:
        """Transfer one potentially blocking close to a dedicated daemon."""
        with self._close_lock:
            if self._close_claimed:
                return
            closer = threading.Thread(
                target=_close_cancelled_pipe,
                args=(self._stream, self._close_in_worker, self._close_result),
                name=f'{self._thread.name}-cancel',
                daemon=True,
            )
            closer.start()
            self._close_claimed = True
            self._cancellation_started = True

    def _claim_pipe(self) -> bool:
        """Claim the owned pipe for exactly one close attempt."""
        with self._close_lock:
            if self._close_claimed:
                return False
            self._close_claimed = True
            return True

    def _close_claimed_pipe(self) -> None:
        """Close a pipe after this worker transferred its ownership."""
        if self._close_in_worker:
            _close_stdin_pipe(self._stream)
        else:
            self._stream.close()

    def _close_pipe(self) -> None:
        """Close a normally joined worker's pipe synchronously."""
        if self._claim_pipe():
            self._close_claimed_pipe()

    def _failure(self) -> Exception | None:
        """Return the first worker-target or cancellation-close failure."""
        return self._target_error or self._close_result._error

    def _is_alive(self) -> bool:
        """Return whether this worker has not stopped."""
        return self._thread.is_alive()

    def _join(self, timeout: float) -> None:
        """Wait up to the caller's share of the cleanup deadline."""
        if self._thread.ident is not None:
            self._thread.join(timeout)

    def _observe_cancellation(self, timeout: float) -> None:
        """Wait up to one bounded share for cancellation result delivery."""
        if self._cancellation_started:
            self._close_result._completed.wait(timeout)

    def _run(self) -> None:
        """Run the pipe operation while retaining unexpected failure."""
        cleanup = CleanupState()
        cleanup.attempt(self._target)
        if self._close_in_worker and self._claim_pipe():
            cleanup.attempt(self._close_claimed_pipe)
        self._target_error = cleanup.first_error

    def _start(self) -> None:
        """Start this daemon worker."""
        self._thread.start()


def _close_stdin_pipe(stream: _WorkerStream) -> None:
    """Close stdin without promoting a child's broken pipe to failure."""
    try:
        stream.close()
    except OSError as error:
        if not is_downstream_closure(error, windows=sys.platform == 'win32'):
            raise


def _close_cancelled_pipe(
    stream: _WorkerStream,
    broken_pipe_is_benign: bool,
    result: _CancellationClose,
) -> None:
    """Close one cancelled pipe while retaining non-benign failures."""
    try:
        cleanup = CleanupState()
        if broken_pipe_is_benign:
            with cleanup:
                _close_stdin_pipe(stream)
        else:
            cleanup.attempt(stream.close)
        result._error = cleanup.first_error
    finally:
        result._completed.set()


def _is_windows_crash(value: int) -> bool:
    """Classify one exact Windows exit value as a likely crash."""
    unsigned = value & 0xFFFFFFFF
    return (
        _WINDOWS_CRASH_RANGE_START <= unsigned <= _WINDOWS_CRASH_RANGE_END
        or _WINDOWS_RUNTIME_CRASH_RANGE_START <= unsigned <= _WINDOWS_RUNTIME_CRASH_RANGE_END
        or unsigned in _WINDOWS_CRASH_CODES
    )


def _read_stderr(stream: StderrReader, relay: StderrRelay) -> None:
    """Drain child stderr through its bounded diagnostic relay."""
    while data := stream.read1(64 * 1024):
        relay.feed(data)
    relay.finish()


def _read_stdout(
    stream: BinaryIO,
    output: bytearray,
    exceeded: threading.Event,
) -> None:
    """Drain child stdout while retaining no more than its byte budget."""
    while True:
        remaining = max(0, _STDOUT_LIMIT_BYTES - len(output))
        data = stream.read(min(64 * 1024, remaining + 1))
        if not data:
            return
        output.extend(data[:remaining])
        if len(data) > remaining:
            exceeded.set()
            return


def _write_stdin(stream: BinaryIO, input_bytes: bytes) -> None:
    """Deliver input without promoting a child's closed pipe to failure."""
    try:
        stream.write(input_bytes)
    except OSError as error:
        if not is_downstream_closure(error, windows=sys.platform == 'win32'):
            raise
