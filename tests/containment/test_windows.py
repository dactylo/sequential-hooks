import ast
import builtins
import contextlib
import ctypes
import errno
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from sequential_hooks import _executor
from sequential_hooks._containment import (
    CompletionProcess,
    ContainmentBackend,
    ContainmentError,
    ContainmentReleaseError,
    ManagedStep,
)
from sequential_hooks._containment import _windows as windows_module
from sequential_hooks._containment import _windows_api as windows_api_module
from sequential_hooks._containment._uncontained import UncontainedBackend
from sequential_hooks._containment._windows import (
    _CREATE_NEW_PROCESS_GROUP,
    _LAUNCHER_STATUS_LIMIT_BYTES,
    WindowsBackend,
    _LauncherProtocolError,
    _WindowsManagedStep,
)
from sequential_hooks._containment._windows_api import (
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    NativeWindowsApi,
)
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._executor import ProcessExecutor, _is_windows_crash
from sequential_hooks._types import ContainmentState, FailureKind, Step, StepResult

_ARGV_LIMIT_BYTES = 1024 * 1024
_LAUNCHER = (
    Path(__file__).parents[2]
    / 'src'
    / 'sequential_hooks'
    / '_containment'
    / '_windows_launcher.py'
)
_STEP = Step(1, (sys.executable, '-c', 'pass'))


class _FakeLauncherProcess:
    def __init__(
        self,
        control_fd: int,
        status_fd: int,
        factory: '_FakePopenFactory',
    ) -> None:
        self._control_fd = control_fd
        self._events = factory.events
        self._status_fd = status_fd
        self._status_frame = factory.status_frame
        self._target_returncode = factory.returncode
        self._wait_errors = factory.wait_errors
        self._factory = factory
        self.control_frame = b''
        self.pid = 4242
        self.returncode: int | None = None
        self.stdin, self.stdout, self.stderr = factory.standard_streams or (
            _TrackingBytesIO(),
            _TrackingBytesIO(),
            _TrackingBytesIO(),
        )

    def _close_child_handles(self) -> None:
        descriptors = (self._control_fd, self._status_fd)
        self._control_fd = -1
        self._status_fd = -1
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _complete(self) -> None:
        if self.returncode is not None:
            return
        control = bytearray()
        while chunk := os.read(self._control_fd, 4096):
            control.extend(chunk)
        self.control_frame = bytes(control)
        os.close(self._control_fd)
        if control:
            self._events.append('release')
            os.write(self._status_fd, self._status_frame)
        os.close(self._status_fd)
        self.returncode = self._target_returncode

    def kill(self) -> None:
        self._events.append('kill')
        if not self._factory.kill_has_effect:
            return
        self._close_child_handles()
        self.returncode = 1

    def poll(self) -> int | None:
        self._complete()
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self._events.append(f'signal:{signum}')

    def terminate(self) -> None:
        self._events.append('terminate-process')
        if not self._factory.terminate_has_effect:
            return
        self._close_child_handles()
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        self._events.append('wait')
        if self._factory.wait_always_times_out:
            raise subprocess.TimeoutExpired('launcher', timeout or 0.0)
        if self._wait_errors:
            raise self._wait_errors.pop(0)
        self._complete()
        assert self.returncode is not None
        return self.returncode


class _FallbackBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Step, bool]] = []

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        self.calls.append((step, allow_uncontained))
        return cast('ManagedStep', SimpleNamespace(containment=ContainmentState.UNCONTAINED))

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        del failed
        return self.start(step, allow_uncontained=allow_uncontained)


class _JobCompletionKernel:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.closed: list[int] = []
        self.active_until = 0.0
        self.close_failures: list[int] = []
        self.dequeue_seconds = 0.0
        self.departed = ''
        self.failure = ''
        self.messages: list[tuple[int, int, int]] = []
        self.now = 0.0
        self.opened: list[int] = []
        self.waits: list[tuple[int, int]] = []
        self.last_error = 0
        monkeypatch.setattr(
            windows_api_module,
            'time',
            SimpleNamespace(monotonic=lambda: self.now),
        )
        monkeypatch.setattr(ctypes, 'get_last_error', lambda: self.last_error, raising=False)

    def close(self, handle: int) -> bool:
        self.closed.append(handle)
        self.last_error = handle
        return handle not in self.close_failures

    def dequeue(
        self,
        port: int,
        message_pointer: object,
        key_pointer: object,
        pid_pointer: object,
        milliseconds: int,
    ) -> bool:
        assert port == 7001
        self.now += self.dequeue_seconds
        if self.failure == 'queue':
            self.last_error = 5
            return False
        if not self.messages:
            self.now += milliseconds / 1000
            self.last_error = 258
            return False
        message, key, pid = self.messages.pop(0)
        cast('SimpleNamespace', message_pointer)._obj.value = message
        cast('SimpleNamespace', key_pointer)._obj.value = key
        cast('SimpleNamespace', pid_pointer)._obj.value = pid
        return True

    def in_job(self, process: int, job: int, result_pointer: object) -> bool:
        assert job == 9001
        cast('SimpleNamespace', result_pointer)._obj.value = not (
            process == 101 and self.departed == 'reused'
        )
        return self.failure != 'identity'

    def open_process(self, rights: int, inherit: bool, pid: int) -> int:
        assert rights == 0x00100000 | 0x1000
        assert inherit is False
        if self.failure == 'open':
            self.last_error = 5
            return 0
        if pid == 101 and self.departed == 'destroyed':
            self.last_error = 87
            return 0
        self.opened.append(pid)
        return pid

    def query(
        self,
        job: int,
        information_class: int,
        information_pointer: object,
        information_size: int,
        return_length: object,
    ) -> bool:
        assert job == 9001
        assert information_class == 1
        assert return_length is None
        assert information_size == ctypes.sizeof(
            windows_api_module._JobObjectBasicAccountingInformation,
        )
        information = cast('SimpleNamespace', information_pointer)._obj
        information.ActiveProcesses = int(self.now < self.active_until)
        information.TotalProcesses = 2
        return self.failure != 'query'

    def wait(self, handle: int, milliseconds: int) -> int:
        self.waits.append((handle, milliseconds))
        self.now += min(0.25, milliseconds / 1000)
        if self.failure == 'native-wait':
            return 0xFFFFFFFF
        return 258 if self.failure == 'wait-timeout' else 0

    def api(self) -> NativeWindowsApi:
        api = object.__new__(NativeWindowsApi)
        api._jobs = {
            9001: {
                '_completed': False,
                '_observed_processes': 0,
                '_port_handle': 7001,
                '_process_handles': [],
            },
        }
        api._kernel32 = SimpleNamespace(
            CloseHandle=self.close,
            GetQueuedCompletionStatus=self.dequeue,
            IsProcessInJob=self.in_job,
            OpenProcess=self.open_process,
            QueryInformationJobObject=self.query,
            WaitForSingleObject=self.wait,
        )
        return api


class _LauncherOs:
    O_BINARY = 0
    O_RDONLY = 0
    O_WRONLY = 0

    def __init__(self, control: bytes) -> None:
        self._reads = {10: bytearray(control)}
        self.closed: list[int] = []
        self.writes = bytearray()

    def close(self, fd: int) -> None:
        self.closed.append(fd)

    def read(self, fd: int, count: int) -> bytes:
        data = self._reads.setdefault(fd, bytearray())
        result = bytes(data[:count])
        del data[:count]
        return result

    def write(self, fd: int, data: bytes) -> int:
        assert fd == 11
        self.writes.extend(data)
        return len(data)


class _RealFallbackBackend(_FallbackBackend):
    def __init__(self, observe_start: Callable[[], None]) -> None:
        super().__init__()
        self._observe_start = observe_start

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        self.calls.append((step, allow_uncontained))
        self._observe_start()
        return UncontainedBackend().start(
            step,
            allow_uncontained=allow_uncontained,
        )


class _RefusingFallbackBackend(_FallbackBackend):
    def start(self, step: Step, *, allow_uncontained: bool) -> NoReturn:
        self.calls.append((step, allow_uncontained))
        raise ContainmentError('fallback started before positive launcher disposal')


class _TrackingBytesIO(BytesIO):
    def __init__(self, value: bytes = b'') -> None:
        super().__init__(value)
        self.close_count = 0
        self.close_error: OSError | None = None

    def close(self) -> None:
        self.close_count += 1
        error = self.close_error
        self.close_error = None
        super().close()
        if error is not None:
            raise error


class _LockedWorkerStream(_TrackingBytesIO):
    def __init__(self, io_release: threading.Event) -> None:
        super().__init__()
        self.close_before_io_release: list[bool] = []
        self.io_started = threading.Event()
        self._io_release = io_release

    def close(self) -> None:
        closed_early = not self._io_release.is_set()
        self.close_before_io_release.append(closed_early)
        if closed_early:
            self._io_release.set()
        self._io_release.wait(timeout=5.0)
        super().close()

    def read(self, _size: int | None = -1) -> bytes:
        self.io_started.set()
        self._io_release.wait(timeout=5.0)
        return b''

    def read1(self, size: int | None = -1) -> bytes:
        return self.read(size)

    def write(self, data: object) -> int:
        self.io_started.set()
        self._io_release.wait(timeout=5.0)
        return len(cast('bytes', data))


def _assert_descriptor_closed(descriptor: int) -> None:
    with pytest.raises(OSError, match=r'.+') as exc_info:
        os.fstat(descriptor)
    assert exc_info.value.errno == errno.EBADF


def _close_managed_pipes(managed: ManagedStep) -> None:
    for stream in (managed.stdin, managed.stdout, managed.stderr):
        stream.close()


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _empty_calls() -> list[tuple[tuple[str, ...], dict[str, object]]]:
    return []


def _empty_exceptions() -> list[Exception]:
    return []


@dataclass
class _FakePopenFactory:
    events: list[str]
    returncode: int = 0
    status_frame: bytes = b'\x00\x00\x00\x00\x00'
    error: OSError | None = None
    missing_stream: str | None = None
    standard_streams: tuple[_TrackingBytesIO, _TrackingBytesIO, _TrackingBytesIO] | None = None
    kill_has_effect: bool = True
    terminate_has_effect: bool = True
    wait_always_times_out: bool = False
    wait_errors: list[Exception] = field(default_factory=_empty_exceptions)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = field(default_factory=_empty_calls)
    process: _FakeLauncherProcess | None = None

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        self.events.append('launcher')
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        startup_info = cast('dict[str, tuple[int, int]]', kwargs['startupinfo'])
        control_handle, status_handle = startup_info['handle_list']
        self.process = _FakeLauncherProcess(
            os.dup(control_handle),
            os.dup(status_handle),
            self,
        )
        if self.missing_stream is not None:
            setattr(self.process, self.missing_stream, None)
        return cast('subprocess.Popen[bytes]', self.process)


def _empty_ints() -> list[int]:
    return []


@dataclass
class _FakeWindowsApi:
    events: list[str]
    assignment_error: OSError | None = None
    close_callback: Callable[[], None] | None = None
    close_error: OSError | None = None
    creation_error: OSError | None = None
    ctrl_break_error: OSError | None = None
    ctrl_break_result: bool = True
    terminate_error: OSError | None = None
    terminate_callback: Callable[[], None] | None = None
    closed: list[int] = field(default_factory=_empty_ints)
    job_wait_callback: Callable[[float], bool] | None = None

    def assign_pid(self, job_handle: int, pid: int) -> None:
        self.events.append(f'assign:{job_handle}:{pid}')
        if self.assignment_error is not None:
            raise self.assignment_error

    def close_handle(self, handle: int) -> None:
        self.events.append(f'close-job:{handle}')
        self.closed.append(handle)
        if self.close_callback is not None:
            self.close_callback()
        if self.close_error is not None:
            raise self.close_error

    def create_kill_on_close_job(self) -> int:
        self.events.append('create-job')
        if self.creation_error is not None:
            raise self.creation_error
        return 9001

    def generate_ctrl_break(self, process_group_id: int) -> bool:
        self.events.append(f'ctrl-break:{process_group_id}')
        if self.ctrl_break_error is not None:
            raise self.ctrl_break_error
        return self.ctrl_break_result

    def terminate_job(self, job_handle: int) -> None:
        self.events.append(f'terminate-job:{job_handle}')
        if self.terminate_callback is not None:
            self.terminate_callback()
        if self.terminate_error is not None:
            raise self.terminate_error

    def wait_job(self, job_handle: int, timeout_seconds: float) -> bool:
        self.events.append(f'wait-job:{job_handle}')
        if self.job_wait_callback is not None:
            return self.job_wait_callback(timeout_seconds)
        return True

    def wait_process(self, process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        raise AssertionError(f'unexpected process wait: {process.pid}, {timeout_seconds}')


def _backend(
    api: _FakeWindowsApi,
    popen: _FakePopenFactory,
    *,
    fallback: _FallbackBackend | None = None,
    write_fd: Callable[[int, bytes], int] = os.write,
) -> WindowsBackend:

    def identity_fd(fd: int) -> int:
        return fd

    def set_inheritable(handle: int, *, inheritable: bool) -> None:
        os.set_inheritable(handle, inheritable)

    def startup_info(handles: tuple[int, int]) -> dict[str, tuple[int, int]]:
        return {'handle_list': handles}

    return WindowsBackend(
        api,
        runtime=vars(windows_module)['_WindowsRuntime'](
            _fd_to_handle=identity_fd,
            _popen_factory=popen,
            _set_handle_inheritable=set_inheritable,
            _startup_info_factory=startup_info,
            _write_fd=write_fd,
        ),
        uncontained_backend=fallback,
    )


def _execute_launcher(
    control: bytes,
    *,
    ctypes_module: object | None = None,
    failed_import: str | None = None,
    os_factory: Callable[[bytes], _LauncherOs] | None = None,
    popen_factory: Callable[..., object] | None = None,
) -> _LauncherOs:
    launcher_os = (_LauncherOs if os_factory is None else os_factory)(control)
    launcher_sys = SimpleNamespace(argv=['launcher', '10', '11'])

    def exit_process(_value: int) -> NoReturn:
        raise SystemExit(0)

    def win_dll(_name: str, *, use_last_error: bool) -> object:
        del use_last_error
        return SimpleNamespace(ExitProcess=exit_process)

    launcher_ctypes = ctypes_module or SimpleNamespace(
        WinDLL=win_dll,
        c_uint32=int,
    )

    def ignore_signal(*_args: object) -> None:
        return None

    def open_osfhandle(handle: int, _flags: int) -> int:
        return handle

    def unexpected_popen(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail('real step unexpectedly started')

    launcher_msvcrt = SimpleNamespace(open_osfhandle=open_osfhandle)
    launcher_signal = SimpleNamespace(SIGBREAK=21, signal=ignore_signal)
    launcher_subprocess = SimpleNamespace(Popen=popen_factory or unexpected_popen)
    modules: dict[str, object] = {
        'ctypes': launcher_ctypes,
        'json': json,
        'msvcrt': launcher_msvcrt,
        'os': launcher_os,
        'signal': launcher_signal,
        'subprocess': launcher_subprocess,
        'sys': launcher_sys,
    }
    original_import = builtins.__import__

    def import_module(name: str, *args: object, **kwargs: object) -> object:
        del args, kwargs
        if name == failed_import:
            raise ImportError(f'test refused {name}')
        if name in modules:
            return modules[name]
        return original_import(name)

    launcher_builtins = dict(vars(builtins))
    launcher_builtins['__import__'] = import_module
    namespace: dict[str, object] = {
        '__builtins__': launcher_builtins,
        '__name__': '__main__',
    }
    execute = cast(
        'Callable[[object, dict[str, object]], None]',
        launcher_builtins['exec'],
    )
    with contextlib.suppress(SystemExit):
        execute(compile(_LAUNCHER.read_bytes(), str(_LAUNCHER), 'exec'), namespace)
    return launcher_os


def _join_control_writers() -> None:
    for writer in threading.enumerate():
        if writer.name.startswith('sequential-hooks-control-writer-'):
            writer.join(timeout=1.0)


def _run_executor(
    monkeypatch: pytest.MonkeyPatch,
    backend: ContainmentBackend,
    *,
    allow_uncontained: bool = False,
) -> StepResult:
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    return ProcessExecutor(Diagnostics(StringIO())).run(
        _STEP,
        b'{}\n',
        None,
        allow_uncontained=allow_uncontained,
    )


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_assignment_failure_never_releases_without_override(
    allow_uncontained: bool,
) -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events, assignment_error=OSError('assignment refused'))
    popen = _FakePopenFactory(events)
    backend = _backend(api, popen)

    if allow_uncontained:
        managed = backend.start(_STEP, allow_uncontained=True)
        assert managed.containment is ContainmentState.UNCONTAINED
        assert 'release' not in events
        managed.release()
        managed.process.wait()
        managed.finish()
        _close_managed_pipes(managed)
        assert events.index('assign:9001:4242') < events.index('release')
    else:
        with pytest.raises(ContainmentError, match='assignment'):
            backend.start(_STEP, allow_uncontained=False)
        assert 'release' not in events
    assert api.closed == [9001]


def test_backend_assigns_before_release_and_inherits_only_gate_handles() -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(events)
    managed = _backend(api, popen).start(_STEP, allow_uncontained=False)

    assert events == ['create-job', 'launcher', 'assign:9001:4242']
    command, kwargs = popen.calls[0]
    assert command == (
        sys.executable,
        '-I',
        '-S',
        str(_LAUNCHER.resolve()),
        command[4],
        command[5],
    )
    assert '-m' not in command
    assert Path(command[3]).is_absolute()
    assert cast('int', kwargs['creationflags']) & _CREATE_NEW_PROCESS_GROUP
    inherited = cast('dict[str, tuple[int, int]]', kwargs['startupinfo'])['handle_list']
    assert 9001 not in inherited
    assert kwargs['close_fds'] is True
    assert events[-1].startswith('assign:')

    managed.release()
    managed.process.wait()
    managed.finish()
    _close_managed_pipes(managed)

    assert events.index('assign:9001:4242') < events.index('release')
    assert events.index('terminate-job:9001') < events.index('close-job:9001')
    assert api.closed == [9001]
    assert popen.process is not None
    frame = popen.process.control_frame
    assert frame[:1] == b'R'
    argv_length = int.from_bytes(frame[1:9], 'little')
    assert json.loads(frame[9 : 9 + argv_length]) == list(_STEP.argv)
    assert b'{}\n' not in frame


def test_bounded_release_transfers_only_small_writer_ownership() -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(events)
    unblock = threading.Event()
    release_errors: list[Exception] = []

    def blocked_write(_fd: int, _data: bytes) -> int:
        unblock.wait()
        raise OSError('injected permanent block released')

    managed = cast(
        '_WindowsManagedStep',
        _backend(api, popen, write_fd=blocked_write).start(
            _STEP,
            allow_uncontained=False,
        ),
    )
    assert popen.process is not None
    api.terminate_callback = popen.process.terminate

    def release() -> None:
        try:
            managed.release()
        except (ContainmentReleaseError, _LauncherProtocolError, OSError) as error:
            release_errors.append(error)

    caller = threading.Thread(target=release, name='test-release-caller')
    started = time.monotonic()
    caller.start()
    caller.join(timeout=3.0)
    elapsed = time.monotonic() - started
    writers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith('sequential-hooks-control-writer-')
    ]
    try:
        assert not caller.is_alive()
        assert elapsed < 3.0
        assert release_errors
        assert isinstance(release_errors[0], ContainmentReleaseError)
        assert 'terminate-job:9001' in events
        assert writers
        for writer in writers:
            target = cast('Callable[..., object]', vars(writer)['_target'])
            assert target.__closure__ is None
            assert all(
                not isinstance(value, (_WindowsManagedStep, ProcessExecutor))
                for value in cast('tuple[object, ...]', vars(writer)['_args'])
            )
    finally:
        unblock.set()
        caller.join(timeout=1.0)
        for writer in writers:
            writer.join(timeout=1.0)
        with contextlib.suppress(Exception):
            managed.abort_before_release()
        _close_managed_pipes(managed)


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_control_write_failure_disposes_launcher_before_recovery(
    allow_uncontained: bool,
) -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(events)
    fallback = _FallbackBackend()

    def fail_write(_fd: int, _data: bytes) -> int:
        raise OSError('control write failed')

    backend = _backend(api, popen, fallback=fallback, write_fd=fail_write)
    managed = backend.start(_STEP, allow_uncontained=allow_uncontained)
    with pytest.raises(ContainmentReleaseError):
        managed.release()
    managed.abort_before_release()
    if allow_uncontained:
        recovered = backend.recover_release_failure(
            _STEP,
            managed,
            allow_uncontained=True,
        )
        assert recovered.containment is ContainmentState.UNCONTAINED
        assert fallback.calls == [(_STEP, True)]
    else:
        with pytest.raises(ContainmentError, match='release'):
            backend.recover_release_failure(
                _STEP,
                managed,
                allow_uncontained=False,
            )
        assert fallback.calls == []
    _close_managed_pipes(managed)
    assert 'release' not in events
    assert events.count('close-job:9001') == 1


def test_control_write_status_invariant_is_not_override_eligible() -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(
        events,
        status_frame=b'\x03\x09\x00\x00\x00bad frame',
    )
    fallback = _FallbackBackend()
    write_calls = 0

    def write_partial_then_fail(fd: int, data: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return os.write(fd, data[:1])
        raise OSError('control write failed after readiness')

    managed = _backend(
        api,
        popen,
        fallback=fallback,
        write_fd=write_partial_then_fail,
    ).start(_STEP, allow_uncontained=True)

    with pytest.raises(_LauncherProtocolError, match='invariant'):
        managed.release()

    managed.abort_before_release()
    _close_managed_pipes(managed)
    assert fallback.calls == []


def test_executor_closes_each_parent_standard_pipe_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    popen = _FakePopenFactory(events)
    result = _run_executor(
        monkeypatch,
        _backend(_FakeWindowsApi(events), popen),
    )

    assert result.returncode == 0
    assert popen.process is not None
    for stream in (popen.process.stdin, popen.process.stdout, popen.process.stderr):
        assert stream.close_count == 1


def test_executor_failed_release_reap_never_starts_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    gate_pipes: list[tuple[int, int, int, int]] = []
    write_release = threading.Event()
    real_create_gate_pipes = windows_module._create_gate_pipes

    def capture_gate_pipes() -> tuple[int, int, int, int]:
        pipes = real_create_gate_pipes()
        gate_pipes.append(pipes)
        return pipes

    def blocked_write(_fd: int, _data: bytes) -> int:
        write_release.wait(timeout=5.0)
        raise OSError('injected blocked control write released')

    monkeypatch.setattr(windows_module, '_create_gate_pipes', capture_gate_pipes)
    fallback = _RefusingFallbackBackend()
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(
        events,
        kill_has_effect=False,
        terminate_has_effect=False,
        wait_always_times_out=True,
    )
    release_errors: list[_LauncherProtocolError] = []
    try:
        try:
            _run_executor(
                monkeypatch,
                _backend(
                    api,
                    popen,
                    fallback=fallback,
                    write_fd=blocked_write,
                ),
                allow_uncontained=True,
            )
        except _LauncherProtocolError as error:
            release_errors.append(error)

        assert release_errors
        assert 'reap' in str(release_errors[0])
        assert fallback.calls == []
        assert api.closed == [9001]
        assert popen.process is not None
        assert popen.process.returncode is None
        _, _, status_reader, _ = gate_pipes[0]
        _assert_descriptor_closed(status_reader)
        for stream in (popen.process.stdin, popen.process.stdout, popen.process.stderr):
            assert stream.close_count == 1
    finally:
        write_release.set()
        if popen.process is not None:
            close_child_handles = type(popen.process)._close_child_handles
            close_child_handles(popen.process)
        _join_control_writers()
        for pipes in gate_pipes:
            for descriptor in pipes:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def test_executor_installs_sigbreak_when_runtime_exposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[int] = []
    sigbreak = 999
    monkeypatch.setattr(_executor.signal, 'SIGBREAK', sigbreak, raising=False)

    def current_handler(signum: int) -> int:
        return signum

    monkeypatch.setattr(_executor.signal, 'getsignal', current_handler)

    def record_handler(signum: int, _handler: object) -> None:
        installed.append(signum)

    monkeypatch.setattr(_executor.signal, 'signal', record_handler)
    executor = ProcessExecutor(Diagnostics(StringIO()))

    with executor.signal_forwarding():
        pass

    assert installed.count(sigbreak) == 2


@pytest.mark.parametrize('returncode', [0, 259, 0x80000000, 0xC0000005, 0xFFFFFFFF])
def test_executor_preserves_unsigned_transport_before_crash_interpretation(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    events: list[str] = []
    backend = _backend(
        _FakeWindowsApi(events),
        _FakePopenFactory(events, returncode=returncode),
    )

    result = _run_executor(monkeypatch, backend)

    assert result.returncode == returncode
    assert (result.failure is not None) is _is_windows_crash(returncode)


def test_executor_release_failure_leaves_standard_pipes_to_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    io_release = threading.Event()
    write_release = threading.Event()
    streams = (
        _LockedWorkerStream(io_release),
        _LockedWorkerStream(io_release),
        _LockedWorkerStream(io_release),
    )
    fallback_observations: list[tuple[bool, tuple[int, int, int]]] = []

    def observe_fallback() -> None:
        events.append('fallback')
        fallback_observations.append(
            (
                'old-disposed' in events,
                (
                    streams[0].close_count,
                    streams[1].close_count,
                    streams[2].close_count,
                ),
            )
        )

    fallback = _RealFallbackBackend(observe_fallback)
    api = _FakeWindowsApi(events, close_callback=io_release.set)
    popen = _FakePopenFactory(events, standard_streams=streams)

    def terminate_launcher() -> None:
        assert popen.process is not None
        popen.process.terminate()
        if 'old-disposed' in events:
            return
        events.append('old-disposed')

    def blocked_write(_fd: int, _data: bytes) -> int:
        assert all(stream.io_started.wait(timeout=1.0) for stream in streams)
        write_release.wait(timeout=5.0)
        raise OSError('injected blocked control write released')

    api.terminate_callback = terminate_launcher
    backend = _backend(
        api,
        popen,
        fallback=fallback,
        write_fd=blocked_write,
    )
    started = time.monotonic()
    try:
        result = _run_executor(
            monkeypatch,
            backend,
            allow_uncontained=True,
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 0
        assert result.containment is ContainmentState.UNCONTAINED
        assert elapsed < 4.0
        assert fallback_observations == [(True, (1, 1, 1))]
        for stream in streams:
            assert stream.close_count == 1
            assert stream.close_before_io_release == [False]
    finally:
        io_release.set()
        write_release.set()
        _join_control_writers()
        for stream in streams:
            if not stream.closed:
                stream.close()


def test_executor_thread_start_failure_retains_gate_for_non_overrideable_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    gate_pipes: list[tuple[int, int, int, int]] = []
    real_create_gate_pipes = windows_module._create_gate_pipes

    def capture_gate_pipes() -> tuple[int, int, int, int]:
        pipes = real_create_gate_pipes()
        gate_pipes.append(pipes)
        return pipes

    def refuse_start() -> NoReturn:
        raise RuntimeError('injected control writer start failure')

    def thread_factory(
        *,
        target: Callable[..., object],
        args: tuple[object, ...],
        name: str,
        daemon: bool,
    ) -> object:
        del target, args, daemon
        assert name.startswith('sequential-hooks-control-writer-')
        return SimpleNamespace(start=refuse_start)

    monkeypatch.setattr(windows_module, '_create_gate_pipes', capture_gate_pipes)
    monkeypatch.setattr(
        windows_module,
        'threading',
        SimpleNamespace(Event=threading.Event, Thread=thread_factory),
    )
    fallback = _FallbackBackend()
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(
        events,
        wait_errors=[subprocess.TimeoutExpired('launcher', 0.01)],
    )

    def terminate_launcher() -> None:
        assert popen.process is not None
        popen.process.terminate()

    api.terminate_callback = terminate_launcher
    try:
        with pytest.raises(_LauncherProtocolError, match='writer'):
            _run_executor(
                monkeypatch,
                _backend(api, popen, fallback=fallback),
                allow_uncontained=True,
            )

        assert len(gate_pipes) == 1
        _, control_writer, status_reader, _ = gate_pipes[0]
        for descriptor in (control_writer, status_reader):
            _assert_descriptor_closed(descriptor)
        assert fallback.calls == []
        assert api.closed == [9001]
        assert popen.process is not None
        assert popen.process.returncode == 1
        for stream in (popen.process.stdin, popen.process.stdout, popen.process.stderr):
            assert stream.close_count == 1
    finally:
        for pipes in gate_pipes:
            for descriptor in pipes:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def test_job_cleanup_exhaustion_is_reported_without_resetting_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    now = [10.0]
    waits: list[float] = []

    def exhaust_deadline(timeout: float) -> bool:
        waits.append(timeout)
        now[0] += timeout
        return False

    api = _FakeWindowsApi(events, job_wait_callback=exhaust_deadline)
    managed = _backend(api, _FakePopenFactory(events)).start(_STEP, allow_uncontained=False)
    managed.release()
    managed.process.wait()
    monkeypatch.setattr(windows_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    try:
        with pytest.raises(ContainmentError, match=r'job.*completion'):
            managed.terminate_tree(0.0)
        with pytest.raises(ContainmentError, match=r'job.*completion'):
            managed.finish()
        assert waits == [windows_module._FORCE_KILL_WAIT_SECONDS, 0.0]
        assert events.count('terminate-job:9001') == 1
        assert api.closed == [9001]
    finally:
        managed.finish()
        _close_managed_pipes(managed)


@pytest.mark.parametrize('operation', ['finish', 'terminate_tree'])
@pytest.mark.parametrize('launcher_exited', [False, True])
def test_job_cleanup_waits_for_descendants_independently_of_launcher(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    launcher_exited: bool,
) -> None:
    events: list[str] = []
    descendants_alive = True

    def complete_descendants(timeout: float) -> bool:
        nonlocal descendants_alive
        assert timeout > 0
        assert api.closed == []
        assert 'terminate-job:9001' in events
        descendants_alive = False
        return True

    api = _FakeWindowsApi(events, job_wait_callback=complete_descendants)
    popen = _FakePopenFactory(events)
    managed = _backend(api, popen).start(_STEP, allow_uncontained=False)
    managed.release()
    assert popen.process is not None
    if launcher_exited:
        managed.process.wait()
    else:
        process = popen.process
        monkeypatch.setattr(process, 'poll', lambda: process.returncode)
        if operation == 'terminate_tree':
            popen.wait_errors.append(subprocess.TimeoutExpired('launcher', 0.0))
        api.terminate_callback = process.terminate
    try:
        if operation == 'finish':
            managed.finish()
        else:
            managed.terminate_tree(0.0)
        assert not descendants_alive
    finally:
        managed.finish()
        _close_managed_pipes(managed)


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_job_creation_failure_obeys_override(
    allow_uncontained: bool,
) -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events, creation_error=OSError('job refused'))
    fallback = _FallbackBackend()
    backend = _backend(api, _FakePopenFactory(events), fallback=fallback)

    if allow_uncontained:
        managed = backend.start(_STEP, allow_uncontained=True)
        assert managed.containment is ContainmentState.UNCONTAINED
        assert fallback.calls == [(_STEP, True)]
    else:
        with pytest.raises(ContainmentError, match='Job Object'):
            backend.start(_STEP, allow_uncontained=False)
        assert fallback.calls == []
    assert events == ['create-job']


@pytest.mark.parametrize('failure_stage', ['terminate', 'wait'])
def test_job_finish_preserves_first_error_and_closes_handle(failure_stage: str) -> None:
    first_error = OSError('first job cleanup failure')

    def fail_wait(_timeout: float) -> bool:
        raise first_error

    api = _FakeWindowsApi(
        [],
        close_error=OSError('later close failure'),
        terminate_error=first_error if failure_stage == 'terminate' else None,
        job_wait_callback=fail_wait,
    )
    managed = _backend(api, _FakePopenFactory(api.events)).start(_STEP, allow_uncontained=False)
    managed.release()
    managed.process.wait()
    try:
        with pytest.raises(OSError, match=r'job cleanup failure|close failure') as caught:
            managed.finish()
        assert caught.value is first_error
        assert api.closed == [9001]
        managed.finish()
        assert api.closed == [9001]
    finally:
        _close_managed_pipes(managed)


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_launcher_creation_failure_closes_job_before_override(
    allow_uncontained: bool,
) -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    fallback = _FallbackBackend()
    popen = _FakePopenFactory(events, error=OSError('launcher refused'))
    backend = _backend(api, popen, fallback=fallback)

    if allow_uncontained:
        managed = backend.start(_STEP, allow_uncontained=True)
        assert managed.containment is ContainmentState.UNCONTAINED
        assert fallback.calls == [(_STEP, True)]
    else:
        with pytest.raises(ContainmentError, match='launcher'):
            backend.start(_STEP, allow_uncontained=False)
        assert fallback.calls == []
    assert events.index('launcher') < events.index('close-job:9001')


def test_launcher_facade_satisfies_completion_process() -> None:
    events: list[str] = []
    popen = _FakePopenFactory(events)
    managed = _backend(_FakeWindowsApi(events), popen).start(
        _STEP,
        allow_uncontained=False,
    )

    try:
        assert isinstance(managed.process, CompletionProcess)
        managed.release()
        assert managed.process.wait(timeout=1.0) == 0
    finally:
        managed.finish()
        assert popen.process is not None
        _close_process_pipes(cast('subprocess.Popen[bytes]', popen.process))


def test_launcher_first_statement_is_the_control_read() -> None:
    tree = ast.parse(_LAUNCHER.read_text(encoding='utf-8'))
    first_index = int(ast.get_docstring(tree, clean=False) is not None)
    first = tree.body[first_index]
    assert isinstance(first, ast.Assign)
    assert isinstance(first.value, ast.Call)
    assert ast.unparse(first.value).startswith("__import__('os').read(")
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '__import__'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith('sequential_hooks')
        )
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize('release', [b'', b'X'])
def test_launcher_invalid_release_aborts_silently(release: bytes) -> None:
    launcher_os = _execute_launcher(release)
    assert launcher_os.writes == b''
    assert launcher_os.closed == [10]


@pytest.mark.parametrize(
    'status_frame',
    [
        b'',
        b'\x04\x00\x00\x00\x00',
        b'\x03\x01\x00\x00\x00\xff',
        b'\x01\x05\x00\x00\x00bad',
        b'\x01\x03\x00\x00\x00badtrailing',
        b'\x03\x03\x00\x00\x00bad',
        b'\x00\x01\x00\x00\x00x',
        b'\x01' + _LAUNCHER_STATUS_LIMIT_BYTES.to_bytes(4, 'little'),
    ],
)
def test_launcher_protocol_corruption_is_never_overridden(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
) -> None:
    events: list[str] = []
    fallback = _FallbackBackend()
    backend = _backend(
        _FakeWindowsApi(events),
        _FakePopenFactory(events, status_frame=status_frame),
        fallback=fallback,
    )

    with pytest.raises(_LauncherProtocolError):
        _run_executor(monkeypatch, backend, allow_uncontained=True)

    assert fallback.calls == []


def test_launcher_rejects_trailing_control_bytes_before_child_start() -> None:
    argv_json = b'["tool"]'
    popen_calls: list[str] = []

    def record_popen(*_args: object, **_kwargs: object) -> NoReturn:
        popen_calls.append('popen')
        raise OSError('child start must follow complete frame validation')

    launcher_os = _execute_launcher(
        b'R' + len(argv_json).to_bytes(8, 'little') + argv_json + b'trailing',
        popen_factory=record_popen,
    )

    assert launcher_os.writes[0] == 3
    assert popen_calls == []


def test_launcher_relays_child_exit_when_status_close_fails() -> None:
    argv_json = b'["tool"]'
    recorded: list[int] = []

    def exit_process(value: int) -> NoReturn:
        recorded.append(value)
        raise SystemExit(0)

    def win_dll(_name: str, *, use_last_error: bool) -> object:
        del use_last_error
        return SimpleNamespace(ExitProcess=exit_process)

    class _StatusCloseFailingOs(_LauncherOs):
        def close(self, fd: int) -> None:
            super().close(fd)
            if fd == 11:
                raise OSError('status close failed')

    class _FakeChild:
        def wait(self) -> int:
            return 7

    launcher_os = _execute_launcher(
        b'R' + len(argv_json).to_bytes(8, 'little') + argv_json,
        ctypes_module=SimpleNamespace(WinDLL=win_dll, c_uint32=ctypes.c_uint32),
        popen_factory=lambda *_args, **_kwargs: _FakeChild(),
        os_factory=_StatusCloseFailingOs,
    )

    assert launcher_os.writes[0] == 0
    assert recorded == [7]


@pytest.mark.parametrize(
    'frame',
    [
        b'R',
        b'R' + (1024 * 1024 + 1).to_bytes(8, 'little'),
        b'R' + (1).to_bytes(8, 'little') + b'\xff',
        b'R' + (2).to_bytes(8, 'little') + b'{}',
        b'R' + (2).to_bytes(8, 'little') + b'[]',
        b'R' + (3).to_bytes(8, 'little') + b'[1]',
        b'R' + (5).to_bytes(8, 'little') + b'["x"]trailing',
    ],
)
def test_launcher_reports_every_post_readiness_malformed_frame(frame: bytes) -> None:
    launcher_os = _execute_launcher(frame)
    assert launcher_os.writes[0] == 3
    declared = int.from_bytes(launcher_os.writes[1:5], 'little')
    assert 0 < declared <= _LAUNCHER_STATUS_LIMIT_BYTES - 5
    assert len(launcher_os.writes) == declared + 5
    assert 10 in launcher_os.closed
    assert 11 in launcher_os.closed


def test_launcher_reports_exit_transport_setup_failure_before_child_start() -> None:
    argv_json = b'["tool"]'
    popen_calls: list[str] = []

    def refuse_win_dll(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError('WinDLL refused by test')

    def record_popen(*_args: object, **_kwargs: object) -> NoReturn:
        popen_calls.append('popen')
        raise OSError('child start must follow exit setup')

    launcher_os = _execute_launcher(
        b'R' + len(argv_json).to_bytes(8, 'little') + argv_json,
        ctypes_module=SimpleNamespace(WinDLL=refuse_win_dll, c_uint32=int),
        popen_factory=record_popen,
    )

    assert launcher_os.writes[0] == 3
    assert popen_calls == []


def test_launcher_reports_post_readiness_import_failure() -> None:
    launcher_os = _execute_launcher(b'R', failed_import='subprocess')
    assert launcher_os.writes[0] == 3
    declared = int.from_bytes(launcher_os.writes[1:5], 'little')
    assert 0 < declared <= _LAUNCHER_STATUS_LIMIT_BYTES - 5
    assert len(launcher_os.writes) == declared + 5
    assert 10 in launcher_os.closed
    assert 11 in launcher_os.closed


@pytest.mark.parametrize(
    ('error', 'expected_status'),
    [
        (FileNotFoundError('missing'), 1),
        (OSError('spawn failed'), 2),
        (ValueError('embedded null character'), 2),
    ],
)
def test_launcher_reports_real_step_spawn_failures_on_status_channel(
    error: Exception,
    expected_status: int,
) -> None:
    argv_json = b'["missing-tool"]'
    calls: list[dict[str, object]] = []

    def fail_popen(*_args: object, **kwargs: object) -> NoReturn:
        calls.append(kwargs)
        raise error

    launcher_os = _execute_launcher(
        b'R' + len(argv_json).to_bytes(8, 'little') + argv_json,
        popen_factory=fail_popen,
    )

    assert launcher_os.writes[0] == expected_status
    message_length = int.from_bytes(launcher_os.writes[1:5], 'little')
    assert message_length > 0
    assert len(launcher_os.writes) == message_length + 5
    assert calls == [
        {
            'stdin': 0,
            'stdout': 1,
            'stderr': 2,
            'shell': False,
            'close_fds': True,
        }
    ]


def test_missing_launcher_standard_pipe_is_wrapper_invariant_without_override() -> None:
    events: list[str] = []
    api = _FakeWindowsApi(events)
    fallback = _FallbackBackend()
    popen = _FakePopenFactory(events, missing_stream='stdout')

    with pytest.raises(_LauncherProtocolError, match='standard pipes'):
        _backend(api, popen, fallback=fallback).start(
            _STEP,
            allow_uncontained=True,
        )

    assert fallback.calls == []
    assert popen.process is not None
    assert popen.process.returncode is not None
    assert popen.process.stdin.closed
    assert popen.process.stderr.closed
    assert events.count('close-job:9001') == 1
    assert 'terminate-process' in events
    assert 'wait' in events


def test_native_job_close_attempts_every_owner_and_preserves_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (6, 9001, 102)]
    api = kernel.api()
    assert api.wait_job(9001, 1.0)
    kernel.close_failures = [101, 102, 7001, 9001]

    with pytest.raises(OSError, match='CloseHandle') as caught:
        api.close_handle(9001)
    assert caught.value.errno == 101
    assert kernel.closed == [101, 102, 7001, 9001]
    assert api._jobs == {}


def test_native_job_wait_bounds_a_busy_notification_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(7, 9001, 101)] * 100
    kernel.dequeue_seconds = 0.25
    api = kernel.api()

    assert not api.wait_job(9001, 1.0)
    assert kernel.now == 1.0
    assert len(kernel.messages) == 96
    api.close_handle(9001)


@pytest.mark.parametrize(
    'failure',
    ['missing-event', 'wait-timeout', 'query', 'open', 'identity', 'queue', 'native-wait'],
)
def test_native_job_wait_cannot_report_incomplete_capture_or_teardown(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (6, 9001, 102)]
    kernel.failure = failure
    if failure == 'missing-event':
        kernel.messages.pop()
    api = kernel.api()

    if failure in ('missing-event', 'wait-timeout'):
        assert not api.wait_job(9001, 1.0)
    else:
        with pytest.raises(OSError, match='failed'):
            api.wait_job(9001, 1.0)
    api.close_handle(9001)
    assert 7001 in kernel.closed
    assert 9001 in kernel.closed
    assert all(pid in kernel.closed for pid in kernel.opened)


def test_native_job_wait_captures_late_birth_without_resetting_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (4, 9001, 0), (6, 9001, 102)]
    api = kernel.api()

    assert api.wait_job(9001, 1.0)
    assert kernel.opened == [101, 102]
    assert kernel.waits == [(101, 1000), (102, 750)]
    api.close_handle(9001)


@pytest.mark.parametrize('zero_notification', [False, True])
def test_native_job_wait_confirms_handles_after_accounting_zero(
    monkeypatch: pytest.MonkeyPatch,
    zero_notification: bool,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (6, 9001, 102)]
    if zero_notification:
        kernel.messages.append((4, 9001, 0))
    api = kernel.api()

    assert api.wait_job(9001, 1.0)
    assert kernel.waits == [(101, 1000), (102, 750)]
    assert kernel.closed == []
    assert api.wait_job(9001, 0.0)
    assert len(kernel.waits) == 2
    api.close_handle(9001)
    assert sorted(kernel.closed) == [101, 102, 7001, 9001]


@pytest.mark.parametrize('departed', ['destroyed', 'reused'])
def test_native_job_wait_does_not_wait_on_reused_or_destroyed_process(
    monkeypatch: pytest.MonkeyPatch,
    departed: str,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (6, 9001, 102)]
    kernel.departed = departed
    api = kernel.api()

    assert api.wait_job(9001, 1.0)
    assert kernel.waits == [(102, 1000)]
    api.close_handle(9001)
    assert kernel.closed.count(101) == (1 if departed == 'reused' else 0)


def test_native_job_wait_refreshes_accounting_without_zero_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _JobCompletionKernel(monkeypatch)
    kernel.messages = [(6, 9001, 101), (6, 9001, 102)]
    kernel.active_until = 0.03
    api = kernel.api()

    assert api.wait_job(9001, 1.0)
    assert kernel.waits == [(101, 970), (102, 720)]
    api.close_handle(9001)


@pytest.mark.parametrize('handle', [None, 0, -1, '1234'])
def test_native_process_wait_rejects_missing_or_invalid_owned_handle(handle: object) -> None:
    api = object.__new__(NativeWindowsApi)
    process = cast('subprocess.Popen[bytes]', SimpleNamespace(_handle=handle))

    with pytest.raises(OSError, match='owned Windows process handle'):
        api.wait_process(process, 0.0)


@pytest.mark.parametrize('status', [0xFFFFFFFF, 128])
def test_native_process_wait_reports_native_failure(status: int) -> None:
    def wait(_handle: int, _milliseconds: int) -> int:
        return status

    api = object.__new__(NativeWindowsApi)
    api._kernel32 = SimpleNamespace(WaitForSingleObject=wait)
    process = cast('subprocess.Popen[bytes]', SimpleNamespace(_handle=1234))

    with pytest.raises(OSError, match='WaitForSingleObject'):
        api.wait_process(process, 0.0)


@pytest.mark.parametrize(('status', 'completed'), [(0, True), (258, False)])
def test_native_process_wait_uses_retained_handle_despite_cached_exit(
    status: int,
    completed: bool,
) -> None:
    calls: list[tuple[int, int]] = []

    def wait(handle: int, milliseconds: int) -> int:
        calls.append((handle, milliseconds))
        return status

    api = object.__new__(NativeWindowsApi)
    api._kernel32 = SimpleNamespace(WaitForSingleObject=wait)
    process = cast('subprocess.Popen[bytes]', SimpleNamespace(_handle=1234, returncode=1))

    assert api.wait_process(process, 0.125) is completed
    assert calls == [(1234, 125)]


@pytest.mark.parametrize('failure', ['', 'limits', 'port', 'associate', 'cleanup'])
def test_native_windows_api_sets_kill_on_close_and_tracks_lifetime(failure: str) -> None:
    closed: list[int] = []
    configured: list[int] = []

    def create_job(_security_attributes: object, _name: object) -> int:
        return 17

    def create_port(file: int, existing: object, key: int, concurrency: int) -> int:
        assert file == ctypes.c_void_p(-1).value
        assert existing is None
        assert key == 0
        assert concurrency == 1
        return 0 if failure == 'port' else 18

    def close(handle: int) -> bool:
        closed.append(handle)
        return failure != 'cleanup'

    def set_information(
        job_handle: int,
        information_class: int,
        information_pointer: object,
        information_size: int,
    ) -> bool:
        assert job_handle == 17
        configured.append(information_class)
        information = cast('SimpleNamespace', information_pointer)._obj
        assert information_size == ctypes.sizeof(information)
        if information_class == _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS:
            assert (
                information.BasicLimitInformation.LimitFlags == _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
        else:
            assert information_class == 7
            assert information.CompletionKey == 17
            assert information.CompletionPort == 18
        return not (
            (failure == 'limits' and information_class == 9)
            or (failure in ('associate', 'cleanup') and information_class == 7)
        )

    api = object.__new__(NativeWindowsApi)
    api._jobs = {}
    api._kernel32 = SimpleNamespace(
        CloseHandle=close,
        CreateIoCompletionPort=create_port,
        CreateJobObjectW=create_job,
        SetInformationJobObject=set_information,
    )

    if failure:
        operation = 'CreateIoCompletionPort' if failure == 'port' else 'SetInformationJobObject'
        with pytest.raises(OSError, match=operation):
            api.create_kill_on_close_job()
    else:
        assert api.create_kill_on_close_job() == 17
        assert configured == [9, 7]
        assert closed == []
        api.close_handle(17)
    assert closed == ([17] if failure in ('limits', 'port') else [18, 17])
    assert api._jobs == {}


def test_parent_rejects_oversize_argv_as_non_overrideable_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fallback = _FallbackBackend()

    def reject_any_write(_fd: int, _data: bytes) -> NoReturn:
        raise AssertionError('oversize argv reached the control pipe')

    backend = _backend(
        _FakeWindowsApi(events),
        _FakePopenFactory(events),
        fallback=fallback,
        write_fd=reject_any_write,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    step = Step(1, (sys.executable, 'x' * _ARGV_LIMIT_BYTES))

    with pytest.raises(_LauncherProtocolError, match='argv frame'):
        ProcessExecutor(Diagnostics(StringIO())).run(
            step,
            b'{}\n',
            None,
            allow_uncontained=True,
        )

    assert fallback.calls == []


def test_setup_cleanup_preserves_terminate_error_and_attempts_every_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    terminate_error = OSError('terminate job failed')
    api = _FakeWindowsApi(
        events,
        close_error=OSError('close job failed'),
        terminate_error=terminate_error,
    )
    popen = _FakePopenFactory(
        events,
        wait_errors=[subprocess.TimeoutExpired('launcher', 0.01)],
    )
    managed = cast(
        '_WindowsManagedStep',
        _backend(api, popen).start(_STEP, allow_uncontained=False),
    )
    assert popen.process is not None
    api.terminate_callback = popen.process.terminate
    popen.process.stdout.close_error = OSError('stdout close failed')
    launcher_process = managed._launcher_process
    status_fd = launcher_process._status_reader
    assert status_fd is not None
    original_close = windows_module.os.close

    def fail_status_close(fd: int) -> None:
        if fd == status_fd:
            events.append('close-status')
            raise OSError('status close failed')
        original_close(fd)

    monkeypatch.setattr(windows_module.os, 'close', fail_status_close)
    try:
        assert managed._dispose_setup_failure() is terminate_error
    finally:
        original_close(status_fd)
        popen.process.stdout.close_error = None
        _close_process_pipes(cast('subprocess.Popen[bytes]', popen.process))

    assert 'close-status' in events
    assert api.closed == [9001]
    assert 'terminate-process' in events
    assert events.count('wait') >= 2
    assert popen.process.stdin.close_count == 1
    assert popen.process.stdout.close_count == 1
    assert popen.process.stderr.close_count == 1


def test_setup_cleanup_preserves_wait_error_and_continues_cleanup() -> None:
    events: list[str] = []
    wait_error = RuntimeError('wait inspection failed')
    api = _FakeWindowsApi(events)
    popen = _FakePopenFactory(events, wait_errors=[wait_error])
    managed = cast(
        '_WindowsManagedStep',
        _backend(api, popen).start(_STEP, allow_uncontained=False),
    )
    assert popen.process is not None
    api.terminate_callback = popen.process.terminate

    assert managed._dispose_setup_failure() is wait_error
    assert 'terminate-job:9001' in events
    assert api.closed == [9001]
    assert popen.process.stdin.closed
    assert popen.process.stdout.closed
    assert popen.process.stderr.closed


def test_startup_info_restricts_inherited_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, 'STARTUPINFO', SimpleNamespace, raising=False)

    startup_info = cast(
        'SimpleNamespace',
        windows_module._startup_info((17, 29)),
    )

    assert startup_info.lpAttributeList == {'handle_list': [17, 29]}


@pytest.mark.parametrize(
    ('status_frame', 'kind'),
    [
        (b'\x01\x10\x00\x00\x00real step absent', FailureKind.NOT_FOUND),
        (b'\x02\x0c\x00\x00\x00spawn failed', FailureKind.SPAWN),
    ],
)
def test_valid_launcher_failure_is_not_retried_under_override(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
    kind: FailureKind,
) -> None:
    events: list[str] = []
    fallback = _FallbackBackend()
    backend = _backend(
        _FakeWindowsApi(events),
        _FakePopenFactory(events, status_frame=status_frame),
        fallback=fallback,
    )

    result = _run_executor(monkeypatch, backend, allow_uncontained=True)

    assert result.failure is not None
    assert result.failure.kind is kind
    assert fallback.calls == []
