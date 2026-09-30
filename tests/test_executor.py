import contextlib
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BufferedReader, BytesIO, FileIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, BinaryIO, TextIO, cast

import pytest

from plugins.claude._sequential_hooks._adapter import ClaudeHost
from sequential_hooks import _executor
from sequential_hooks._containment import (
    ContainmentError,
    ContainmentReleaseError,
    ManagedStep,
)
from sequential_hooks._diagnostics import (
    _STDERR_CHAIN_LIMIT_BYTES,
    _STDERR_STEP_LIMIT_BYTES,
    Diagnostics,
    StderrRelay,
)
from sequential_hooks._executable import ExecutableResolution, ExecutableStatus
from sequential_hooks._executor import (
    _OUTPUT_WORKER_JOIN_SECONDS,
    ForwardedSignal,
    ProcessExecutor,
    _is_windows_crash,
)
from sequential_hooks._runner import run_pipeline
from sequential_hooks._types import ContainmentState, FailureKind, Step, StepResult
from tests._helpers import CHILD_TIMEOUT_SECONDS, PROCESS_GROUP_FLAGS, step_transcript

if TYPE_CHECKING:
    from sequential_hooks.hosts import JsonObject, NativeResponse

_FIXTURE = Path(__file__).parent / 'fixtures' / 'hook_child.py'
_STDIN_ERROR_CASES = (
    pytest.param('darwin', BrokenPipeError(errno.EPIPE, 'closed'), True, id='posix-broken-pipe'),
    pytest.param('win32', BrokenPipeError(errno.EPIPE, 'closed'), True, id='windows-broken-pipe'),
    pytest.param('darwin', OSError(errno.EINVAL, 'invalid'), False, id='posix-einval'),
    pytest.param('win32', OSError(errno.EINVAL, 'invalid'), True, id='windows-einval'),
    pytest.param('darwin', OSError(errno.EIO, 'io'), False, id='posix-eio'),
    pytest.param('win32', OSError(errno.EIO, 'io'), False, id='windows-eio'),
)
type _MonitorStep = Callable[
    [ProcessExecutor, ManagedStep, object, float | None],
    tuple[FailureKind | None, Exception | None],
]


class _BlockingDiagnosticStream:
    def __init__(self) -> None:
        self.release_write = threading.Event()
        self.write_finished = threading.Event()
        self.write_started = threading.Event()

    def flush(self) -> None:
        pass

    def write(self, value: str) -> int:
        if not self.write_started.is_set():
            self.write_started.set()
            self.release_write.wait()
            self.write_finished.set()
        return len(value)


class _BlockingPipeBackend:
    def __init__(self) -> None:
        self.managed: _BlockingPipeManagedStep | None = None
        self.started = threading.Event()

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        del step, failed, allow_uncontained
        raise AssertionError('release recovery was not expected')

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        del allow_uncontained
        self.managed = _BlockingPipeManagedStep(step)
        self.started.set()
        return self.managed


class _BlockingPipeManagedStep:
    def __init__(self, step: Step) -> None:
        self.close_finished = threading.Event()
        self.close_threads: list[str] = []
        self.read_started = threading.Event()
        read_fd, self.write_fd = os.pipe()
        raw_reader = FileIO(read_fd, 'rb', closefd=True)
        self._stdout = _RecordingBufferedReader(
            raw_reader,
            self.close_threads,
            self.read_started,
            self.close_finished,
        )
        self._process = subprocess.Popen(
            step.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
        if self._process.stdin is None or self._process.stderr is None:
            raise RuntimeError('blocking-pipe test child pipes were not created')

    @property
    def containment(self) -> ContainmentState:
        return ContainmentState.CONTAINED

    def finish(self) -> None:
        self._process.wait(timeout=0.5)

    def forward_signal(self, signum: int) -> None:
        self._process.send_signal(signum)

    @property
    def process(self) -> subprocess.Popen[bytes]:
        return self._process

    def release(self) -> None:
        pass

    @property
    def stderr(self) -> BufferedReader:
        assert self._process.stderr is not None
        return cast('BufferedReader', self._process.stderr)

    @property
    def stdin(self) -> BinaryIO:
        assert self._process.stdin is not None
        return cast('BinaryIO', self._process.stdin)

    @property
    def stdout(self) -> BinaryIO:
        return self._stdout

    def terminate_tree(self, grace_seconds: float) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        self._process.wait(timeout=grace_seconds)

    def abort_before_release(self) -> None:
        self.terminate_tree(0.1)


class _BufferedChunkStream:
    def __init__(self, chunk: bytes) -> None:
        self.read_calls = 0
        self.read1_calls = 0
        self.release_eof = threading.Event()
        self._chunk = chunk

    def close(self) -> None:
        self.release_eof.set()

    def read(self, _size: int = -1) -> bytes:
        self.read_calls += 1
        self.release_eof.wait(3.0)
        return b''

    def read1(self, _size: int = -1) -> bytes:
        self.read1_calls += 1
        if self._chunk:
            chunk = self._chunk
            self._chunk = b''
            return chunk
        self.release_eof.wait(3.0)
        return b''


class _CleanupFakeAttempt:
    def __init__(self) -> None:
        self._workers = [_CleanupSlowDeadWorker(), _CleanupLiveWorker()]
        self._relay = _CleanupFakeRelay()


class _CleanupFakeRelay:
    def close(self) -> str:
        return ''


class _CleanupLiveWorker:
    def __init__(self) -> None:
        self._error: Exception | None = None

    def _cancel_pipe(self) -> None:
        pass

    def _close_pipe(self) -> None:
        pass

    def _failure(self) -> Exception | None:
        return self._error

    def _is_alive(self) -> bool:
        return True

    def _join(self, timeout: float) -> None:
        del timeout

    def _observe_cancellation(self, timeout: float) -> None:
        if timeout > 0:
            self._error = OSError('late cancellation close failed')


class _CleanupSlowDeadWorker:
    def _cancel_pipe(self) -> None:
        pass

    def _close_pipe(self) -> None:
        time.sleep(0.15)

    def _failure(self) -> Exception | None:
        return None

    def _is_alive(self) -> bool:
        return False

    def _join(self, timeout: float) -> None:
        del timeout

    def _observe_cancellation(self, timeout: float) -> None:
        del timeout


class _CompletedFakeManaged:
    def __init__(self) -> None:
        self.process = _CompletedFakeProcess()
        self.terminations = 0

    def forward_signal(self, signum: int) -> None:
        del signum
        raise AssertionError('no signal expected')

    def terminate_tree(self, grace_seconds: float) -> None:
        del grace_seconds
        self.terminations += 1


class _CompletedFakeProcess:
    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


class _IdleFakeAttempt:
    def __init__(self) -> None:
        self._output_exceeded = threading.Event()

    def _first_worker_error(self) -> Exception | None:
        return None


class _LateInvariantCloseBackend:
    def __init__(self) -> None:
        self.managed: _LateInvariantCloseManagedStep | None = None
        self.started = threading.Event()

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        del step, failed, allow_uncontained
        raise AssertionError('release recovery was not expected')

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        del allow_uncontained
        self.managed = _LateInvariantCloseManagedStep(step)
        self.started.set()
        return self.managed


class _ObservedCancellationEvent:
    def __init__(
        self,
        observation_started: threading.Event,
        release_close: threading.Event,
    ) -> None:
        self._condition = threading.Condition()
        self._observation_started = observation_started
        self._published = False
        self._release_close = release_close

    def set(self) -> None:
        with self._condition:
            self._published = True
            self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._condition:
            self._observation_started.set()
            self._release_close.set()
            return self._condition.wait_for(lambda: self._published, timeout)


class _StdinErrorBackend:
    def __init__(self, *, block_write: bool, close_error: OSError) -> None:
        self.block_write = block_write
        self.close_error = close_error
        self.managed: _StdinErrorManagedStep | None = None

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        del step, failed, allow_uncontained
        raise AssertionError('release recovery was not expected')

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        del allow_uncontained
        self.managed = _StdinErrorManagedStep(
            step,
            block_write=self.block_write,
            close_error=self.close_error,
        )
        return self.managed


class _StdinErrorStream:
    def __init__(
        self,
        delegate: BinaryIO,
        *,
        block_write: bool,
        close_error: OSError | None = None,
        write_error: OSError | None = None,
    ) -> None:
        self.close_attempts = 0
        self.close_started = threading.Event()
        self.release_write = threading.Event()
        self.write_started = threading.Event()
        self._block_write = block_write
        self._close_error = close_error
        self._delegate = delegate
        self._write_error = write_error

    def close(self) -> None:
        self.close_attempts += 1
        self.close_started.set()
        self._delegate.close()
        if self._close_error is not None:
            raise self._close_error

    @property
    def closed(self) -> bool:
        return self._delegate.closed

    def write(self, data: bytes) -> int:
        self.write_started.set()
        if self._block_write:
            self.release_write.wait(3.0)
        if self._write_error is not None:
            raise self._write_error
        return len(data)


class _InvariantError(RuntimeError):
    pass


class _LateInvariantCloseStream:
    def __init__(self, delegate: BinaryIO) -> None:
        self.close_attempts = 0
        self.close_finished = threading.Event()
        self.close_started = threading.Event()
        self.release_close = threading.Event()
        self.release_write = threading.Event()
        self.write_started = threading.Event()
        self._delegate = delegate

    def close(self) -> None:
        self.close_attempts += 1
        self.close_started.set()
        self.release_close.wait(3.0)
        try:
            self._delegate.close()
            raise _InvariantError('late cancellation close failed')
        finally:
            self.close_finished.set()

    @property
    def closed(self) -> bool:
        return self._delegate.closed

    def write(self, data: bytes) -> int:
        self.write_started.set()
        self.release_write.wait(3.0)
        return len(data)


class _PlatformRelativePath(Path):
    def absolute(self) -> Path:
        return Path.cwd() / 'platform-absolute-hook.exe'


class _RecordingBufferedReader(BufferedReader):
    def __init__(
        self,
        raw: FileIO,
        close_threads: list[str],
        read_started: threading.Event,
        close_finished: threading.Event,
    ) -> None:
        super().__init__(raw)
        self._close_finished = close_finished
        self._close_threads = close_threads
        self._read_started = read_started

    def close(self) -> None:
        self._close_threads.append(threading.current_thread().name)
        try:
            super().close()
        finally:
            self._close_finished.set()

    def read(self, size: int | None = -1) -> bytes:
        self._read_started.set()
        return super().read(size)


@dataclass(frozen=True)
class _RecordingOptions:
    containment: ContainmentState = ContainmentState.CONTAINED
    finish_callback: Callable[[], None] | None = None
    finish_error: bool = False
    release_error: bool = False
    release_gate: Path | None = None


class _RecordingManagedStep:
    def __init__(
        self,
        step: Step,
        events: list[str],
        options: _RecordingOptions,
    ) -> None:
        self._containment = options.containment
        self._events = events
        self._finish_callback = options.finish_callback
        self._finish_error = options.finish_error
        self._release_gate = options.release_gate
        argv = (
            (
                sys.executable,
                str(_FIXTURE),
                'gated-side-effect',
                str(options.release_gate),
                step.argv[-2],
                step.argv[-1],
            )
            if options.release_gate is not None
            else step.argv
        )
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            raise RuntimeError('test child pipes were not created')
        self._release_error = options.release_error

    def _stop_process(self, grace_seconds: float) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=CHILD_TIMEOUT_SECONDS)

    def abort_before_release(self) -> None:
        self._events.append('abort')
        self._stop_process(0.1)

    @property
    def containment(self) -> ContainmentState:
        return self._containment

    def finish(self) -> None:
        self._events.append('finish')
        self._stop_process(0.1)
        self._process.returncode = 99
        if self._finish_callback is not None:
            self._finish_callback()
        if self._finish_error:
            raise _InvariantError('finish failed')

    def forward_signal(self, signum: int) -> None:
        self._events.append(f'forward {signum}')
        with contextlib.suppress(ProcessLookupError):
            self._process.send_signal(signum)

    @property
    def process(self) -> subprocess.Popen[bytes]:
        return self._process

    def release(self) -> None:
        self._events.append('release')
        if self._release_error:
            raise ContainmentReleaseError('release failed')
        if self._release_gate is not None:
            self._release_gate.touch()

    @property
    def stderr(self) -> BufferedReader:
        assert self._process.stderr is not None
        return cast('BufferedReader', self._process.stderr)

    @property
    def stdin(self) -> BinaryIO:
        assert self._process.stdin is not None
        return cast('BinaryIO', self._process.stdin)

    @property
    def stdout(self) -> BinaryIO:
        assert self._process.stdout is not None
        return cast('BinaryIO', self._process.stdout)

    def terminate_tree(self, grace_seconds: float) -> None:
        self._events.append('terminate')
        self._stop_process(grace_seconds)


class _StdinErrorManagedStep(_RecordingManagedStep):
    def __init__(self, step: Step, *, block_write: bool, close_error: OSError) -> None:
        super().__init__(step, [], _RecordingOptions())
        self.close_stream = _StdinErrorStream(
            super().stdin,
            block_write=block_write,
            close_error=close_error,
        )

    @property
    def stdin(self) -> BinaryIO:
        return cast('BinaryIO', self.close_stream)


class _LateInvariantCloseManagedStep(_RecordingManagedStep):
    def __init__(self, step: Step) -> None:
        super().__init__(step, [], _RecordingOptions())
        self.close_stream = _LateInvariantCloseStream(super().stdin)

    @property
    def stdin(self) -> BinaryIO:
        return cast('BinaryIO', self.close_stream)


class _SignalingStringIO(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.write_completed = threading.Event()

    def write(self, value: str) -> int:
        result = super().write(value)
        self.write_completed.set()
        return result


def _assert_late_close_failure(
    backend: _LateInvariantCloseBackend,
    errors: list[Exception],
    results: list[StepResult],
    uncaught_errors: list[BaseException],
) -> None:
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], _InvariantError)
    assert str(errors[0]) == 'late cancellation close failed'
    assert backend.managed is not None
    assert backend.managed.close_stream.close_attempts == 1
    assert uncaught_errors == []


def _assert_lifecycle_order(events: list[str], *, terminated: bool) -> None:
    stdout_start = events.index('start stdout')
    stderr_start = events.index('start stderr')
    stdin_start = events.index('start stdin')
    release = events.index('release')
    finish = events.index('finish')
    first_join = min(index for index, event in enumerate(events) if event.startswith('join '))

    assert stdout_start < stderr_start < stdin_start < release
    if terminated:
        assert release < events.index('terminate') < finish
    assert finish < first_join


def _record_thread_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    real_thread = threading.Thread

    class RecordingThread(real_thread):
        def join(self, timeout: float | None = None) -> None:
            events.append(f'join {self.name.rsplit("-", 1)[-1]}')
            super().join(timeout)

        def start(self) -> None:
            events.append(f'start {self.name.rsplit("-", 1)[-1]}')
            super().start()

    monkeypatch.setattr(_executor.threading, 'Thread', RecordingThread)


def _recording_managed_steps() -> list['_RecordingManagedStep']:
    return []


@dataclass
class _RecordingBackend:
    events: list[str]
    finish_error: bool = field(default=False, kw_only=True)
    finish_callback: Callable[[], None] | None = field(default=None, kw_only=True)
    release_error: bool = field(default=False, kw_only=True)
    release_side_effect: Path | None = field(default=None, kw_only=True)
    process_step: Step | None = field(default=None, kw_only=True)
    recovery_process_step: Step | None = field(default=None, kw_only=True)
    managed_steps: 'list[_RecordingManagedStep]' = field(
        default_factory=_recording_managed_steps,
        init=False,
    )
    received_steps: list[Step] = field(default_factory=list[Step], init=False)

    @property
    def release_gate(self) -> Path | None:
        if self.release_side_effect is None:
            return None
        return self.release_side_effect.with_suffix('.release')

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        self.received_steps.append(step)
        assert failed is self.managed_steps[0]
        self.events.append('recover')
        if not allow_uncontained:
            raise ContainmentError('release recovery requires explicit override')
        managed = _RecordingManagedStep(
            self.recovery_process_step or self.process_step or step,
            self.events,
            _RecordingOptions(
                containment=ContainmentState.UNCONTAINED,
                release_gate=self.release_gate,
            ),
        )
        self.managed_steps.append(managed)
        return managed

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        self.received_steps.append(step)
        del allow_uncontained
        self.events.append('start')
        managed = _RecordingManagedStep(
            self.process_step or step,
            self.events,
            _RecordingOptions(
                finish_callback=self.finish_callback,
                finish_error=self.finish_error,
                release_error=self.release_error,
                release_gate=self.release_gate,
            ),
        )
        self.managed_steps.append(managed)
        return managed


def _resolve_test_executable(
    requested: str,
    *,
    path_value: str | None,
    windows: bool,
    pathext_value: str | None,
) -> ExecutableResolution:
    del path_value, windows, pathext_value
    return ExecutableResolution(requested, Path(sys.executable), ExecutableStatus.RUNNABLE)


def _run_fixture(
    mode: str,
    *args: str,
    diagnostics: Diagnostics | None = None,
    input_bytes: bytes = b'{}\n',
    timeout: float | None = CHILD_TIMEOUT_SECONDS,
) -> StepResult:
    selected_diagnostics = diagnostics or Diagnostics(StringIO())
    return ProcessExecutor(selected_diagnostics).run(
        Step(1, (sys.executable, str(_FIXTURE), mode, *args)),
        input_bytes,
        timeout,
        allow_uncontained=False,
    )


def _start_background_call(
    target: Callable[[], object],
) -> tuple[threading.Thread, threading.Event, list[object], list[Exception]]:
    completed = threading.Event()
    errors: list[Exception] = []
    results: list[object] = []

    def run_target() -> None:
        try:
            results.append(target())
        except RuntimeError as error:
            errors.append(error)
        finally:
            completed.set()

    runner = threading.Thread(target=run_target, daemon=True)
    runner.start()
    return runner, completed, results, errors


def _track_relay_close(
    diagnostics: Diagnostics,
    monkeypatch: pytest.MonkeyPatch,
) -> threading.Event:
    real_relay = diagnostics.relay
    relay_closed = threading.Event()

    def tracking_relay(step: Step) -> StderrRelay:
        relay = real_relay(step)
        real_close = relay.close

        def close_relay() -> str:
            captured = real_close()
            relay_closed.set()
            return captured

        monkeypatch.setattr(relay, 'close', close_relay)
        return relay

    monkeypatch.setattr(diagnostics, 'relay', tracking_relay)
    return relay_closed


def test_cancel_pipe_start_failure_keeps_close_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start

    def failing_start(self: threading.Thread) -> None:
        if self.name.endswith('-cancel'):
            raise RuntimeError('no thread available')
        real_start(self)

    monkeypatch.setattr(threading.Thread, 'start', failing_start)
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'rb')
    try:
        worker_type = vars(_executor)['_Worker']
        worker = worker_type(
            name='probe-stdout',
            target=lambda: None,
            stream=stream,
            close_in_worker=False,
        )
        with pytest.raises(RuntimeError):
            worker._cancel_pipe()
        assert not stream.closed
        worker._close_pipe()
        assert stream.closed
    finally:
        os.close(write_fd)
        if not stream.closed:
            stream.close()


def test_cleanup_budget_starts_after_dead_worker_closes() -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))
    cleanup_attempt = cast(
        'Callable[..., tuple[str, bool, Exception | None]]',
        vars(type(executor))['_cleanup_attempt'],
    )

    _stderr, _timed_out, error = cleanup_attempt(
        executor,
        _CleanupFakeAttempt(),
        lambda: None,
        None,
    )

    assert isinstance(error, OSError)


@pytest.mark.parametrize('first_join_elapsed', [0.25, 1.25])
def test_cleanup_shares_one_second_between_worker_joins(
    monkeypatch: pytest.MonkeyPatch,
    first_join_elapsed: float,
) -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))
    now = 100.0
    allowances: list[float] = []
    attempt = _CleanupFakeAttempt()

    def finish_containment() -> None:
        nonlocal now
        now += 5.0

    def join_first(_worker: _CleanupSlowDeadWorker, timeout: float) -> None:
        nonlocal now
        allowances.append(timeout)
        now += first_join_elapsed

    def join_second(_worker: _CleanupLiveWorker, timeout: float) -> None:
        allowances.append(timeout)

    monkeypatch.setattr(_executor, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(_CleanupSlowDeadWorker, '_join', join_first)
    monkeypatch.setattr(attempt._workers[0], '_close_pipe', lambda: None)
    monkeypatch.setattr(_CleanupLiveWorker, '_join', join_second)
    cleanup_attempt = cast(
        'Callable[..., tuple[str, bool, Exception | None]]',
        vars(type(executor))['_cleanup_attempt'],
    )

    cleanup_attempt(executor, attempt, finish_containment, None)

    assert allowances == [1.0, max(0.0, 1.0 - first_join_elapsed)]


@pytest.mark.parametrize(('platform', 'error', 'benign'), _STDIN_ERROR_CASES)
def test_close_stdin_pipe_classifies_closed_pipe_errors(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    error: OSError,
    benign: bool,
) -> None:
    stream = _StdinErrorStream(BytesIO(), block_write=False, close_error=error)
    monkeypatch.setattr(_executor, 'sys', SimpleNamespace(platform=platform))

    if benign:
        _executor._close_stdin_pipe(cast('BinaryIO', stream))
    else:
        with pytest.raises(OSError, match=error.strerror or '') as caught:
            _executor._close_stdin_pipe(cast('BinaryIO', stream))
        assert caught.value is error
    assert stream.close_attempts == 1
    assert stream.closed


def test_executor_accepts_exact_stdout_limit() -> None:
    stream = StringIO()
    result = _run_fixture('stdout-bytes', str(1024 * 1024), diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'stdout-bytes', str(1024 * 1024)),
    )

    assert result.failure is None, result_transcript
    assert len(result.stdout) == 1024 * 1024, result_transcript


def test_executor_bounds_output_worker_join_and_closes_parent_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    events: list[str] = []
    backend = _RecordingBackend(events)
    reader_started = threading.Event()
    reader_finished = threading.Event()
    release_reader = threading.Event()
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def stalled_reader(
        _stream: BinaryIO,
        _output: bytearray,
        _exceeded: threading.Event,
    ) -> None:
        reader_started.set()
        release_reader.wait()
        reader_finished.set()

    monkeypatch.setattr(_executor, '_read_stdout', stalled_reader)
    runner, completed, results, errors = _start_background_call(
        lambda: _run_fixture('exit-without-stdin', diagnostics=Diagnostics(stream))
    )
    try:
        assert reader_started.wait(CHILD_TIMEOUT_SECONDS)
        assert completed.wait(CHILD_TIMEOUT_SECONDS)
        assert not reader_finished.is_set()
        assert errors == []
        result = cast('StepResult', results[0])
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
        )
        assert result.failure is not None, result_transcript
        assert result.failure.kind is FailureKind.OUTPUT_WORKER_TIMEOUT, result_transcript
        assert backend.managed_steps[0].stdout.closed, result_transcript
    finally:
        release_reader.set()
        runner.join(CHILD_TIMEOUT_SECONDS)

    assert not runner.is_alive()
    assert reader_finished.wait(CHILD_TIMEOUT_SECONDS)


def test_executor_cancels_real_blocked_pipe_without_blocking_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _BlockingPipeBackend()
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    runner, completed, results, errors = _start_background_call(
        lambda: _run_fixture('exit-without-stdin')
    )
    try:
        assert backend.started.wait(CHILD_TIMEOUT_SECONDS)
        assert backend.managed is not None
        assert backend.managed.read_started.wait(CHILD_TIMEOUT_SECONDS)
        # Keep the writer open until execution returns: EOF cannot unblock the reader.
        assert completed.wait(CHILD_TIMEOUT_SECONDS)
        assert errors == []
        result = cast('StepResult', results[0])
        assert result.failure is not None
        assert result.failure.kind is FailureKind.OUTPUT_WORKER_TIMEOUT
    finally:
        if backend.managed is not None:
            os.close(backend.managed.write_fd)
        runner.join(CHILD_TIMEOUT_SECONDS)

    assert not runner.is_alive()
    assert backend.managed is not None
    assert backend.managed.close_finished.wait(CHILD_TIMEOUT_SECONDS)
    assert backend.managed.stdout.closed
    assert backend.managed.close_threads == ['sequential-hooks-step-1-stdout-cancel']


def test_executor_cleans_managed_step_when_attempt_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    diagnostics = Diagnostics(StringIO())
    executor = ProcessExecutor(diagnostics)
    relay_closed = _track_relay_close(diagnostics, monkeypatch)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def fail_attempt(*_args: object, **_kwargs: object) -> object:
        raise _InvariantError('attempt construction failed')

    monkeypatch.setattr(_executor, '_Attempt', fail_attempt)
    runner, completed, results, errors = _start_background_call(
        lambda: executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
            b'{}\n',
            None,
            allow_uncontained=False,
        )
    )
    try:
        returned_within_bound = completed.wait(_OUTPUT_WORKER_JOIN_SECONDS + 0.4)
        managed = backend.managed_steps[0]
        process_stopped = managed.process.poll() is not None
        pipes_closed = managed.stdin.closed and managed.stdout.closed and managed.stderr.closed
    finally:
        if backend.managed_steps:
            managed = backend.managed_steps[0]
            managed.abort_before_release()
            managed.stdin.close()
            managed.stdout.close()
            managed.stderr.close()
        runner.join(0.5)

    assert returned_within_bound
    assert not runner.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], _InvariantError)
    assert str(errors[0]) == 'attempt construction failed'
    assert process_stopped
    assert pipes_closed
    assert relay_closed.is_set()


def test_executor_cleans_managed_step_when_relay_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    diagnostics = Diagnostics(StringIO())
    executor = ProcessExecutor(diagnostics)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def fail_relay(_step: Step) -> object:
        raise _InvariantError('relay construction failed')

    monkeypatch.setattr(diagnostics, 'relay', fail_relay)
    runner, completed, results, errors = _start_background_call(
        lambda: executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
            b'{}\n',
            None,
            allow_uncontained=False,
        )
    )
    try:
        returned_within_bound = completed.wait(_OUTPUT_WORKER_JOIN_SECONDS + 0.4)
        managed = backend.managed_steps[0]
        process_stopped = managed.process.poll() is not None
        pipes_closed = managed.stdin.closed and managed.stdout.closed and managed.stderr.closed
    finally:
        if backend.managed_steps:
            managed = backend.managed_steps[0]
            managed.abort_before_release()
            managed.stdin.close()
            managed.stdout.close()
            managed.stderr.close()
        runner.join(0.5)

    assert returned_within_bound
    assert not runner.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], _InvariantError)
    assert str(errors[0]) == 'relay construction failed'
    assert process_stopped
    assert pipes_closed


@pytest.mark.parametrize('failure_index', [1, 2, 3])
def test_executor_cleans_partial_worker_start_failure(
    failure_index: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    diagnostics = Diagnostics(StringIO())
    executor = ProcessExecutor(diagnostics)
    relay_closed = _track_relay_close(diagnostics, monkeypatch)
    worker_type = vars(_executor)['_Worker']
    real_start = cast('Callable[[object], None]', worker_type._start)
    started_threads: list[threading.Thread] = []
    start_count = 0
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def fail_selected_start(worker: object) -> None:
        nonlocal start_count
        start_count += 1
        if start_count == failure_index:
            raise _InvariantError(f'worker start {failure_index} failed')
        real_start(worker)
        started_threads.append(cast('threading.Thread', vars(worker)['_thread']))

    monkeypatch.setattr(worker_type, '_start', fail_selected_start)
    runner, completed, results, errors = _start_background_call(
        lambda: executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
            b'{}\n',
            None,
            allow_uncontained=False,
        )
    )
    try:
        returned_within_bound = completed.wait(_OUTPUT_WORKER_JOIN_SECONDS + 0.4)
        managed = backend.managed_steps[0]
        process_stopped = managed.process.poll() is not None
        pipes_closed = managed.stdin.closed and managed.stdout.closed and managed.stderr.closed
        workers_stopped = all(not thread.is_alive() for thread in started_threads)
    finally:
        if backend.managed_steps:
            managed = backend.managed_steps[0]
            managed.abort_before_release()
            managed.stdin.close()
            managed.stdout.close()
            managed.stderr.close()
        for thread in started_threads:
            thread.join(0.5)
        runner.join(0.5)

    assert returned_within_bound
    assert not runner.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], _InvariantError)
    assert str(errors[0]) == f'worker start {failure_index} failed'
    assert process_stopped
    assert pipes_closed
    assert workers_stopped
    assert relay_closed.is_set()


def test_executor_cleans_partially_acquired_pipes_when_property_access_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    executor = ProcessExecutor(Diagnostics(StringIO()))
    acquired_stdin = BytesIO()
    acquired_stdout = BytesIO()
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def boundary_stdin(_managed: _RecordingManagedStep) -> BinaryIO:
        return acquired_stdin

    def boundary_stdout(_managed: _RecordingManagedStep) -> BinaryIO:
        return acquired_stdout

    def fail_stderr(_managed: _RecordingManagedStep) -> BinaryIO:
        raise _InvariantError('stderr property failed')

    monkeypatch.setattr(_RecordingManagedStep, 'stdin', property(boundary_stdin))
    monkeypatch.setattr(_RecordingManagedStep, 'stdout', property(boundary_stdout))
    monkeypatch.setattr(_RecordingManagedStep, 'stderr', property(fail_stderr))
    runner, completed, results, errors = _start_background_call(
        lambda: executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
            b'{}\n',
            None,
            allow_uncontained=False,
        )
    )
    try:
        returned_within_bound = completed.wait(_OUTPUT_WORKER_JOIN_SECONDS + 0.4)
        runner.join(0.5)

        assert returned_within_bound
        assert not runner.is_alive()
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], _InvariantError)
        assert str(errors[0]) == 'stderr property failed'
        managed = backend.managed_steps[0]
        assert managed.process.poll() is not None
        assert acquired_stdin.closed
        assert acquired_stdout.closed
        assert 'release' not in events
    finally:
        acquired_stdin.close()
        acquired_stdout.close()
        if backend.managed_steps:
            managed = backend.managed_steps[0]
            managed.abort_before_release()
            for stream in (
                managed.process.stdin,
                managed.process.stdout,
                managed.process.stderr,
            ):
                if stream is not None:
                    stream.close()
        runner.join(0.5)


def test_executor_drains_past_stderr_chain_limit_with_one_notice() -> None:
    stream = StringIO()
    diagnostics = Diagnostics(stream)

    for _index in range(_STDERR_CHAIN_LIMIT_BYTES // _STDERR_STEP_LIMIT_BYTES):
        result = _run_fixture(
            'stderr-bytes',
            str(_STDERR_STEP_LIMIT_BYTES),
            diagnostics=diagnostics,
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(
                sys.executable,
                str(_FIXTURE),
                'stderr-bytes',
                str(_STDERR_STEP_LIMIT_BYTES),
            ),
        )
        assert result.failure is None, result_transcript
    exhausted = _run_fixture(
        'stderr-bytes',
        '1',
        diagnostics=diagnostics,
    )
    exhausted_transcript = step_transcript(
        exhausted, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'stderr-bytes', '1')
    )

    assert exhausted.failure is None, exhausted_transcript
    assert exhausted.blocking_stderr == '', exhausted_transcript
    assert stream.getvalue().count('stderr truncated after 4194304 bytes for this chain') == 1, (
        exhausted_transcript
    )


def test_executor_drains_past_stderr_step_limit_with_one_notice() -> None:
    stream = StringIO()

    result = _run_fixture(
        'stderr-bytes',
        str(_STDERR_STEP_LIMIT_BYTES + 1),
        diagnostics=Diagnostics(stream),
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(
            sys.executable,
            str(_FIXTURE),
            'stderr-bytes',
            str(_STDERR_STEP_LIMIT_BYTES + 1),
        ),
    )

    assert result.failure is None, result_transcript
    assert len(result.blocking_stderr.encode()) == _STDERR_STEP_LIMIT_BYTES, result_transcript
    assert stream.getvalue().count('stderr truncated after 1048576 bytes for this step') == 1, (
        result_transcript
    )


def test_executor_fails_closed_when_release_recovery_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = StringIO()
    events: list[str] = []
    side_effect = tmp_path / 'release-side-effect.txt'
    backend = _RecordingBackend(
        events,
        release_error=True,
        release_side_effect=side_effect,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    result = _run_fixture(
        'record-side-effect', str(side_effect), 'original', diagnostics=Diagnostics(stream)
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    assert len(backend.managed_steps) == 1, result_transcript
    assert events.index('abort') < events.index('recover'), result_transcript
    assert not side_effect.exists(), result_transcript
    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.CONTAINMENT, result_transcript


def test_executor_gives_each_call_a_fresh_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))
    now = 100.0
    deadlines: list[float | None] = []
    managed = cast('ManagedStep', _CompletedFakeManaged())

    def start(step: Step, *, allow_uncontained: bool) -> ManagedStep:
        nonlocal now
        del step, allow_uncontained
        now += 5.0
        return managed

    def run_attempt(
        self: ProcessExecutor,
        backend: object,
        managed: ManagedStep,
        request: object,
        deadline: float | None,
    ) -> StepResult:
        nonlocal now
        del self, backend, managed, request
        deadlines.append(deadline)
        now += 20.0
        return StepResult(0, b'', '')

    monkeypatch.setattr(_executor, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(_executor, 'current_backend', lambda: SimpleNamespace(start=start))
    monkeypatch.setattr(_executor, 'resolve_executable', _resolve_test_executable)
    monkeypatch.setattr(ProcessExecutor, '_run_attempt', run_attempt)

    for index, (started, timeout) in enumerate(((100.0, 2.0), (200.0, 12.0)), start=1):
        now = started
        result = executor.run(Step(index, ('hook',)), b'{}\n', timeout, allow_uncontained=False)
        assert result.failure is None
        assert result.returncode == 0

    # Setup precedes each allowance; the prior call's deadline and cleanup do not carry over.
    assert deadlines == [107.0, 217.0]


def test_executor_ignores_broken_pipe_after_early_success() -> None:
    stream = StringIO()
    result = _run_fixture(
        'exit-without-stdin', input_bytes=b'x' * (8 * 1024 * 1024), diagnostics=Diagnostics(stream)
    )
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'exit-without-stdin')
    )

    assert result.failure is None, result_transcript
    assert result.returncode == 0, result_transcript
    assert result.stdout == b'', result_transcript


def test_executor_labels_stderr_without_mixing_it_into_stdout() -> None:
    stream = StringIO()

    result = _run_fixture('stderr', 'warning', diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'stderr', 'warning')
    )

    assert result.failure is None, result_transcript
    assert result.stdout == b'', result_transcript
    assert result.blocking_stderr == 'warning\n', result_transcript
    assert stream.getvalue() == (
        f'sequential-hooks: step 1 ({Path(sys.executable).name}): warning\n'
    ), result_transcript


def test_executor_maps_containment_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class FailingBackend:
        def recover_release_failure(
            self,
            step: Step,
            failed: ManagedStep,
            *,
            allow_uncontained: bool,
        ) -> ManagedStep:
            del step, failed, allow_uncontained
            raise AssertionError('release recovery was not expected')

        def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
            del step, allow_uncontained
            raise ContainmentError('containment unavailable')

    monkeypatch.setattr(_executor, 'current_backend', FailingBackend)

    result = _run_fixture('exit-without-stdin')

    assert result.failure is not None
    assert result.failure.kind is FailureKind.CONTAINMENT


def test_executor_maps_directory_executable_to_not_found(tmp_path: Path) -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))

    result = executor.run(
        Step(1, (str(tmp_path),)),
        b'{}\n',
        None,
        allow_uncontained=False,
    )

    assert result.returncode is None, step_transcript(result)
    assert result.failure is not None
    assert result.failure.kind is FailureKind.NOT_FOUND


def test_executor_maps_missing_executable_to_not_found() -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))

    result = executor.run(
        Step(1, ('/definitely/missing/sequential-hooks-child',)),
        b'{}\n',
        None,
        allow_uncontained=False,
    )

    assert result.returncode is None, step_transcript(result)
    assert result.failure is not None
    assert result.failure.kind is FailureKind.NOT_FOUND


def test_executor_maps_spawn_error_after_static_resolution(tmp_path: Path) -> None:
    suffix = '.exe' if sys.platform == 'win32' else ''
    invalid_executable = tmp_path / f'invalid-executable{suffix}'
    invalid_executable.write_bytes(b'not an executable image')
    invalid_executable.chmod(0o755)

    result = ProcessExecutor(Diagnostics(StringIO())).run(
        Step(1, (str(invalid_executable),)),
        b'{}\n',
        None,
        allow_uncontained=False,
    )

    assert result.returncode is None, step_transcript(result)
    assert result.failure is not None
    assert result.failure.kind is FailureKind.SPAWN


def test_executor_observes_late_cancellation_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LateInvariantCloseBackend()
    errors: list[Exception] = []
    observation_started, relay_close_finished, relay_close_started, release_relay_close = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    results, uncaught_errors = cast(
        'tuple[list[StepResult], list[BaseException]]',
        ([], []),
    )
    real_cancellation_close, real_relay_close = (
        cast('Callable[..., object]', vars(_executor)['_CancellationClose']),
        StderrRelay.close,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def observed_cancellation_close() -> object:
        assert backend.managed is not None
        return real_cancellation_close(
            _completed=cast(
                'threading.Event',
                _ObservedCancellationEvent(
                    observation_started,
                    backend.managed.close_stream.release_close,
                ),
            )
        )

    def gated_relay_close(relay: StderrRelay) -> str:
        relay_close_started.set()
        release_relay_close.wait(3.0)
        try:
            return real_relay_close(relay)
        finally:
            relay_close_finished.set()

    def record_uncaught(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            uncaught_errors.append(args.exc_value)

    def run_executor() -> None:
        try:
            results.append(_run_fixture('exit-without-stdin'))
        except _InvariantError as error:
            errors.append(error)

    monkeypatch.setattr(_executor, '_CancellationClose', observed_cancellation_close)
    monkeypatch.setattr(StderrRelay, 'close', gated_relay_close)
    monkeypatch.setattr(threading, 'excepthook', record_uncaught)
    runner = threading.Thread(target=run_executor)
    runner.start()
    try:
        assert backend.started.wait(0.5)
        assert backend.managed is not None
        close_stream = backend.managed.close_stream
        assert close_stream.write_started.wait(0.5)
        assert close_stream.close_started.wait(2.0)
        assert relay_close_started.wait(0.5)
        release_relay_close.set()
        assert observation_started.wait(0.5)
        assert close_stream.close_finished.wait(0.5)
        runner.join(0.5)
    finally:
        release_relay_close.set()
        if backend.managed is not None:
            backend.managed.close_stream.release_close.set()
            backend.managed.close_stream.release_write.set()
        runner.join(0.5)

    assert not runner.is_alive()
    assert relay_close_finished.is_set()
    _assert_late_close_failure(
        backend,
        errors,
        results,
        uncaught_errors,
    )


def test_executor_orders_worker_exception_cleanup_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    _record_thread_lifecycle(monkeypatch, events)

    def fail_reader(
        _stream: BinaryIO,
        _output: bytearray,
        _exceeded: threading.Event,
    ) -> None:
        raise _InvariantError('stdout reader failed')

    monkeypatch.setattr(_executor, '_read_stdout', fail_reader)

    with pytest.raises(_InvariantError, match='stdout reader failed'):
        _run_fixture('sleep', '10')

    _assert_lifecycle_order(events, terminated=True)
    managed = backend.managed_steps[0]
    assert managed.stdin.closed
    assert managed.stdout.closed
    assert managed.stderr.closed


def test_executor_output_timeout_does_not_block_adapter_failure() -> None:
    stream = _BlockingDiagnosticStream()
    diagnostics = Diagnostics(cast('TextIO', stream))
    raw_input = b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{}}\n'
    payload: JsonObject = {
        'hook_event_name': 'PreToolUse',
        'tool_name': 'Bash',
        'tool_input': {},
    }
    adapter = ClaudeHost().adapter(
        'PreToolUse',
        raw_input,
        payload,
        diagnostics,
        allow_on_failure=False,
    )
    executor = ProcessExecutor(diagnostics)
    step = Step(1, (sys.executable, str(_FIXTURE), 'stderr', 'blocked'))
    runner, completed, results, errors = _start_background_call(
        lambda: run_pipeline(
            adapter,
            (step,),
            executor,
            None,
            allow_on_failure=False,
        )
    )
    try:
        assert stream.write_started.wait(CHILD_TIMEOUT_SECONDS)
        assert completed.wait(CHILD_TIMEOUT_SECONDS)
        assert not stream.write_finished.is_set()
    finally:
        stream.release_write.set()
        runner.join(CHILD_TIMEOUT_SECONDS)

    assert stream.write_finished.wait(CHILD_TIMEOUT_SECONDS)
    assert not runner.is_alive()
    assert errors == []
    assert len(results) == 1
    response = cast('NativeResponse', results[0])
    assert b'output worker timeout' in response.stdout
    assert b'"permissionDecision":"deny"' in response.stdout


def test_executor_preserves_requested_name_in_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    requested = Step(1, ('hook', str(_FIXTURE), 'stderr', 'warning'))
    resolved = Path(sys.executable)
    stream = StringIO()

    def resolve_requested(
        executable: str,
        *,
        path_value: str | None,
        windows: bool,
        pathext_value: str | None,
    ) -> ExecutableResolution:
        del path_value, windows, pathext_value
        return ExecutableResolution(
            executable,
            resolved,
            ExecutableStatus.RUNNABLE,
        )

    monkeypatch.setattr(
        _executor,
        'resolve_executable',
        resolve_requested,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    result = ProcessExecutor(Diagnostics(stream)).run(
        requested,
        b'{}\n',
        CHILD_TIMEOUT_SECONDS,
        allow_uncontained=False,
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    assert result.failure is None, result_transcript
    assert stream.getvalue() == 'sequential-hooks: step 1 (hook): warning\n', result_transcript


def test_executor_preserves_timeout_when_output_limit_arrives_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    events: list[str] = []
    finish_started = threading.Event()
    backend = _RecordingBackend(events, finish_callback=finish_started.set)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def report_timeout(
        _executor_instance: ProcessExecutor,
        _managed: ManagedStep,
        _attempt: object,
        _deadline: float | None,
    ) -> tuple[FailureKind | None, Exception | None]:
        return FailureKind.TIMEOUT, None

    def report_late_output_limit(
        _stream: BinaryIO,
        _output: bytearray,
        exceeded: threading.Event,
    ) -> None:
        assert finish_started.wait(0.5)
        exceeded.set()

    monkeypatch.setattr(ProcessExecutor, '_monitor_step', report_timeout)
    monkeypatch.setattr(_executor, '_read_stdout', report_late_output_limit)

    result = _run_fixture('exit-without-stdin', diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.TIMEOUT, result_transcript


def test_executor_raises_signal_recorded_during_resolution_before_backend_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    executor = ProcessExecutor(Diagnostics(StringIO()))

    def signal_during_resolution(
        executable: str,
        *,
        path_value: str | None,
        windows: bool,
        pathext_value: str | None,
    ) -> ExecutableResolution:
        del path_value, windows, pathext_value
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return ExecutableResolution(
            executable,
            Path(executable),
            ExecutableStatus.RUNNABLE,
        )

    monkeypatch.setattr(_executor, 'resolve_executable', signal_during_resolution)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    with executor.signal_forwarding(), pytest.raises(ForwardedSignal) as raised:
        executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
            b'{}\n',
            None,
            allow_uncontained=False,
        )

    assert raised.value.signum == signal.SIGTERM
    assert events == []


def test_executor_recovers_release_failure_with_fresh_workers_and_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = StringIO()
    events: list[str] = []
    side_effect = tmp_path / 'recovery-side-effect.txt'
    backend = _RecordingBackend(
        events,
        release_error=True,
        release_side_effect=side_effect,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    _record_thread_lifecycle(monkeypatch, events)
    input_bytes = b'{"same":"input"}\n'

    result = ProcessExecutor(Diagnostics(stream)).run(
        Step(
            1,
            (
                sys.executable,
                str(_FIXTURE),
                'record-side-effect',
                str(side_effect),
                'recovered',
            ),
        ),
        input_bytes,
        CHILD_TIMEOUT_SECONDS,
        allow_uncontained=True,
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    first = backend.managed_steps[0]
    assert events.count('start stdout') == 2, result_transcript
    assert events.index('abort') < events.index('recover'), result_transcript
    assert first.stdin.closed, result_transcript
    assert first.stdout.closed, result_transcript
    assert first.stderr.closed, result_transcript
    assert result.failure is None, result_transcript
    assert result.stdout == input_bytes, result_transcript
    assert result.containment is ContainmentState.UNCONTAINED, result_transcript
    assert side_effect.read_text().splitlines() == ['recovered'], result_transcript


def test_executor_rejects_unsupported_executable_before_backend_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correction = 'Use a real executable entry point.'

    def unsupported_resolution(
        executable: str,
        *,
        path_value: str | None,
        windows: bool,
        pathext_value: str | None,
    ) -> ExecutableResolution:
        del path_value, windows, pathext_value
        return ExecutableResolution(
            executable,
            Path(executable),
            ExecutableStatus.UNSUPPORTED_SHIM,
            correction,
        )

    def fail_backend_selection() -> _RecordingBackend:
        raise AssertionError('backend selected for rejected executable')

    monkeypatch.setattr(_executor, 'resolve_executable', unsupported_resolution)
    monkeypatch.setattr(_executor, 'current_backend', fail_backend_selection)

    result = ProcessExecutor(Diagnostics(StringIO())).run(
        Step(1, ('hook.cmd',)),
        b'{}\n',
        None,
        allow_uncontained=False,
    )

    assert result.failure is not None
    assert result.failure.kind is FailureKind.UNSUPPORTED_EXECUTABLE
    assert correction in result.failure.message


def test_executor_replaces_invalid_stderr_and_keeps_result_usable() -> None:
    stream = StringIO()

    result = _run_fixture('invalid-stderr', diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'invalid-stderr')
    )

    assert result.failure is None, result_transcript
    assert result.returncode == 0, result_transcript
    assert result.blocking_stderr == 'bad:\ufffd\n', result_transcript
    assert stream.getvalue() == (
        f'sequential-hooks: step 1 ({Path(sys.executable).name}): '
        '[invalid UTF-8 replaced] bad:\ufffd\n'
    ), result_transcript


def test_executor_reraises_finish_exception_after_all_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events, finish_error=True)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    _record_thread_lifecycle(monkeypatch, events)

    with pytest.raises(_InvariantError, match='finish failed'):
        _run_fixture('exit-without-stdin')

    managed = backend.managed_steps[0]
    assert events.index('finish') < min(
        index for index, event in enumerate(events) if event.startswith('join ')
    )
    assert managed.stdin.closed
    assert managed.stdout.closed
    assert managed.stderr.closed


def test_executor_round_trips_stdin_bytes() -> None:
    stream = StringIO()
    input_bytes = b'{"message":"caf\xc3\xa9"}\n'

    result = _run_fixture('echo-input', input_bytes=input_bytes, diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'echo-input')
    )

    assert result.failure is None, result_transcript
    assert result.returncode == 0, result_transcript
    assert result.stdout == input_bytes, result_transcript


def test_executor_terminates_tree_over_stdout_limit() -> None:
    stream = StringIO()
    result = _run_fixture('stdout-bytes', str(1024 * 1024 + 1), diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'stdout-bytes', str(1024 * 1024 + 1)),
    )

    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.OUTPUT_LIMIT, result_transcript
    assert len(result.stdout) == 1024 * 1024, result_transcript


def test_executor_uses_platform_absolute_path_for_relative_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(
        events,
        release_error=True,
        process_step=Step(
            1,
            (sys.executable, str(_FIXTURE), 'sleep', '10'),
        ),
        recovery_process_step=Step(
            1,
            (sys.executable, str(_FIXTURE), 'stderr', 'warning'),
        ),
    )
    requested = Step(1, ('hook',))
    resolved = _PlatformRelativePath(r'C:bin\hook.exe')
    stream = StringIO()

    def resolve_requested(
        executable: str,
        *,
        path_value: str | None,
        windows: bool,
        pathext_value: str | None,
    ) -> ExecutableResolution:
        del path_value, windows, pathext_value
        return ExecutableResolution(
            executable,
            resolved,
            ExecutableStatus.RUNNABLE,
        )

    monkeypatch.setattr(
        _executor,
        'resolve_executable',
        resolve_requested,
    )
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    result = ProcessExecutor(Diagnostics(stream)).run(
        requested,
        b'{}\n',
        CHILD_TIMEOUT_SECONDS,
        allow_uncontained=True,
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    launch_step = Step(1, (str(Path.cwd() / 'platform-absolute-hook.exe'),))
    assert result.returncode == 0, result_transcript
    assert result.failure is None, result_transcript
    assert backend.received_steps == [launch_step, launch_step], result_transcript
    assert stream.getvalue() == 'sequential-hooks: step 1 (hook): warning\n', result_transcript


def test_monitor_step_prefers_completion_over_expired_deadline() -> None:
    executor = ProcessExecutor(Diagnostics(StringIO()))
    managed = _CompletedFakeManaged()
    monitor_step = cast(
        '_MonitorStep',
        vars(ProcessExecutor)['_monitor_step'],
    )

    failure_kind, worker_error = monitor_step(
        executor,
        cast('ManagedStep', managed),
        _IdleFakeAttempt(),
        time.monotonic() - 1.0,
    )

    assert failure_kind is None
    assert worker_error is None
    assert managed.terminations == 0


def test_signal_context_forwards_only_first_signal_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    executor = ProcessExecutor(Diagnostics(StringIO()))
    handled_signals = tuple(
        signum
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK')
        if (signum := getattr(signal, name, None)) is not None
    )
    previous_handlers = {signum: object() for signum in handled_signals}
    installed_handlers: dict[int, Callable[[int, object], None]] = {}
    transitions: list[tuple[int, object]] = []
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    def previous_handler(signum: int) -> object:
        return previous_handlers[signum]

    monkeypatch.setattr(
        _executor.signal,
        'getsignal',
        previous_handler,
    )

    def install_handler(signum: int, handler: object) -> None:
        transitions.append((signum, handler))
        if callable(handler):
            installed_handlers[signum] = cast('Callable[[int, object], None]', handler)

    monkeypatch.setattr(_executor.signal, 'signal', install_handler)
    start = backend.start

    def start_with_signals(step: Step, *, allow_uncontained: bool) -> ManagedStep:
        managed = start(step, allow_uncontained=allow_uncontained)
        installed_handlers[signal.SIGTERM](signal.SIGTERM, None)
        installed_handlers[signal.SIGINT](signal.SIGINT, None)
        return managed

    monkeypatch.setattr(backend, 'start', start_with_signals)

    def run_with_signals() -> None:
        with executor.signal_forwarding():
            executor.run(
                Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
                b'{}\n',
                CHILD_TIMEOUT_SECONDS,
                allow_uncontained=False,
            )

    with pytest.raises(ForwardedSignal) as raised:
        run_with_signals()

    assert raised.value.signum == signal.SIGTERM
    assert events.count(f'forward {signal.SIGTERM}') == 1
    assert all(event != f'forward {signal.SIGINT}' for event in events)
    assert events.count('terminate') == 1
    for signum in handled_signals:
        assert transitions[-len(handled_signals) :].count((signum, previous_handlers[signum])) == 1


def test_signal_forwarding_prefers_pending_signal_over_restoration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_signals = tuple(
        signum
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK')
        if (signum := getattr(signal, name, None)) is not None
    )
    previous_handlers = {signum: object() for signum in handled_signals}
    installed_handlers: dict[int, Callable[[int, object], None]] = {}

    def previous_handler(signum: int) -> object:
        return previous_handlers[signum]

    def fail_restoration(signum: int, handler: object) -> None:
        if handler is previous_handlers[signum]:
            raise OSError('restore failed')
        installed_handlers[signum] = cast('Callable[[int, object], None]', handler)

    monkeypatch.setattr(signal, 'getsignal', previous_handler)
    monkeypatch.setattr(signal, 'signal', fail_restoration)
    executor = ProcessExecutor(Diagnostics(StringIO()))

    with pytest.raises(ForwardedSignal) as raised, executor.signal_forwarding():
        installed_handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert raised.value.signum == signal.SIGTERM


def test_signal_forwarding_restores_remaining_after_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_signals = tuple(
        signum
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK')
        if (signum := getattr(signal, name, None)) is not None
    )
    previous_handlers = {signum: object() for signum in handled_signals}
    installation_complete = False
    restored: list[int] = []

    def previous_handler(signum: int) -> object:
        return previous_handlers[signum]

    def recording_signal(signum: int, _handler: object) -> None:
        if not installation_complete:
            return
        restored.append(signum)
        if len(restored) == 1:
            raise OSError('restore failed')

    monkeypatch.setattr(signal, 'getsignal', previous_handler)
    monkeypatch.setattr(signal, 'signal', recording_signal)
    executor = ProcessExecutor(Diagnostics(StringIO()))

    with pytest.raises(OSError, match='restore failed'), executor.signal_forwarding():
        installation_complete = True

    assert restored == list(handled_signals)


def test_signal_forwarding_skips_none_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installs: list[int] = []

    def no_previous_handler(_signum: int) -> None:
        return None

    def record_install(signum: int, _handler: object) -> None:
        installs.append(signum)

    monkeypatch.setattr(signal, 'getsignal', no_previous_handler)
    monkeypatch.setattr(signal, 'signal', record_install)
    executor = ProcessExecutor(Diagnostics(StringIO()))

    with executor.signal_forwarding():
        install_count = len(installs)

    # None previous handlers are skipped: no further signal.signal calls on exit.
    assert len(installs) == install_count


def test_stderr_reader_forwards_buffered_chunk_before_eof() -> None:
    stream = _BufferedChunkStream(b'available now\n')
    output = _SignalingStringIO()
    relay = Diagnostics(output).relay(Step(1, ('hook',)))
    read_stderr = _executor._read_stderr
    reader = threading.Thread(
        target=read_stderr,
        args=(stream, relay),
        daemon=True,
    )

    reader.start()
    forwarded_before_eof = output.write_completed.wait(0.5)
    stream.release_eof.set()
    reader.join(0.5)

    assert forwarded_before_eof
    assert not reader.is_alive()
    assert stream.read_calls == 0
    assert stream.read1_calls == 2
    assert relay.close() == 'available now\n'
    assert output.getvalue() == 'sequential-hooks: step 1 (hook): available now\n'


@pytest.mark.parametrize(('platform', 'error', 'benign'), _STDIN_ERROR_CASES)
def test_write_stdin_classifies_closed_pipe_errors(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    error: OSError,
    benign: bool,
) -> None:
    stream = _StdinErrorStream(BytesIO(), block_write=False, write_error=error)
    monkeypatch.setattr(_executor, 'sys', SimpleNamespace(platform=platform))

    if benign:
        _executor._write_stdin(cast('BinaryIO', stream), b'input')
    else:
        with pytest.raises(OSError, match=error.strerror or '') as caught:
            _executor._write_stdin(cast('BinaryIO', stream), b'input')
        assert caught.value is error
    assert stream.write_started.is_set()


@pytest.mark.parametrize(
    ('platform', 'error', 'benign'),
    [
        pytest.param('win32', OSError(errno.EINVAL, 'invalid'), True, id='windows-einval'),
        pytest.param('darwin', OSError(errno.EIO, 'io'), False, id='posix-eio'),
    ],
)
def test_executor_classifies_normal_stdin_owner_close_error(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    error: OSError,
    benign: bool,
) -> None:
    stream = StringIO()
    backend = _StdinErrorBackend(block_write=False, close_error=error)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    monkeypatch.setattr(_executor, 'resolve_executable', _resolve_test_executable)
    monkeypatch.setattr(_executor, 'sys', SimpleNamespace(platform=platform))

    if benign:
        result = _run_fixture('exit-without-stdin', diagnostics=Diagnostics(stream))
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), 'exit-without-stdin'),
        )
        assert result.failure is None, result_transcript
        assert result.returncode == 0, result_transcript
    else:
        with pytest.raises(OSError, match=error.strerror or '') as caught:
            _run_fixture('exit-without-stdin')
        assert caught.value is error

    assert backend.managed is not None
    assert backend.managed.close_stream.close_attempts == 1
    assert backend.managed.close_stream.closed


@pytest.mark.parametrize(
    ('platform', 'error', 'benign'),
    [
        pytest.param('win32', OSError(errno.EINVAL, 'invalid'), True, id='windows-einval'),
        pytest.param('darwin', OSError(errno.EIO, 'io'), False, id='posix-eio'),
    ],
)
def test_executor_classifies_timed_out_stdin_cancel_close_error(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    error: OSError,
    benign: bool,
) -> None:
    stream = StringIO()
    backend = _StdinErrorBackend(block_write=True, close_error=error)
    uncaught_errors: list[BaseException] = []
    uncaught = threading.Event()
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    monkeypatch.setattr(_executor, 'resolve_executable', _resolve_test_executable)
    monkeypatch.setattr(_executor, 'sys', SimpleNamespace(platform=platform))

    def record_uncaught(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            uncaught_errors.append(args.exc_value)
        uncaught.set()

    monkeypatch.setattr(threading, 'excepthook', record_uncaught)
    try:
        if benign:
            result = _run_fixture('exit-without-stdin', diagnostics=Diagnostics(stream))
            result_transcript = step_transcript(
                result,
                stream.getvalue(),
                command=(sys.executable, str(_FIXTURE), 'exit-without-stdin'),
            )
            assert result.failure is not None, result_transcript
            assert result.failure.kind is FailureKind.OUTPUT_WORKER_TIMEOUT, result_transcript
        else:
            with pytest.raises(OSError, match=error.strerror or '') as caught:
                _run_fixture('exit-without-stdin')
            assert caught.value is error
        assert backend.managed is not None
        assert backend.managed.close_stream.write_started.is_set()
        assert backend.managed.close_stream.close_started.wait(0.2)
        uncaught.wait(0.1)
    finally:
        if backend.managed is not None:
            backend.managed.close_stream.release_write.set()

    assert backend.managed is not None
    assert backend.managed.close_stream.close_attempts == 1
    assert backend.managed.close_stream.closed
    assert uncaught_errors == []


@pytest.mark.skipif(os.name != 'posix', reason='negative signal return codes are POSIX-specific')
def test_executor_maps_child_signal_to_crash() -> None:
    stream = StringIO()
    result = _run_fixture('self-signal', str(signal.SIGTERM), diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'self-signal', str(signal.SIGTERM)),
    )

    assert result.returncode == -signal.SIGTERM, result_transcript
    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.CRASH, result_transcript
    assert result.failure.returncode == -signal.SIGTERM, result_transcript


@pytest.mark.parametrize(
    'scenario',
    [
        ('exit-without-stdin', (), None, None, False),
        ('sleep', ('10',), 0.05, FailureKind.TIMEOUT, True),
        (
            'stdout-bytes-then-sleep',
            (str(1024 * 1024 + 1), '10'),
            0.5,
            FailureKind.OUTPUT_LIMIT,
            True,
        ),
    ],
)
def test_executor_orders_managed_lifecycle_before_worker_joins(
    monkeypatch: pytest.MonkeyPatch,
    scenario: tuple[
        str,
        tuple[str, ...],
        float | None,
        FailureKind | None,
        bool,
    ],
) -> None:
    stream = StringIO()
    mode, args, timeout, failure_kind, terminated = scenario
    events: list[str] = []
    backend = _RecordingBackend(events)
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)
    _record_thread_lifecycle(monkeypatch, events)

    result = _run_fixture(
        mode,
        *args,
        timeout=CHILD_TIMEOUT_SECONDS if timeout is None else timeout,
        diagnostics=Diagnostics(stream),
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[-1].process.args),
    )

    assert (result.failure.kind if result.failure is not None else None) is failure_kind, (
        result_transcript
    )
    if failure_kind is None:
        assert result.returncode == 0, result_transcript
    _assert_lifecycle_order(events, terminated=terminated)


@pytest.mark.parametrize('returncode', [1, 2])
def test_executor_preserves_nonzero_status_and_stderr_for_adapter(returncode: int) -> None:
    stream = StringIO()
    result = _run_fixture('exit', str(returncode), 'blocked', diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'exit', str(returncode), 'blocked'),
    )

    assert result.failure is None, result_transcript
    assert result.returncode == returncode, result_transcript
    assert result.stdout == b'', result_transcript
    assert result.blocking_stderr == 'blocked\n', result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='signal forwarding uses POSIX signals')
def test_executor_raises_signal_between_steps_before_backend_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = _RecordingBackend(events)
    stream = StringIO()
    executor = ProcessExecutor(Diagnostics(stream))
    monkeypatch.setattr(_executor, 'current_backend', lambda: backend)

    with executor.signal_forwarding():
        first = executor.run(
            Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
            b'{}\n',
            CHILD_TIMEOUT_SECONDS,
            allow_uncontained=False,
        )
        os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(ForwardedSignal) as raised:
            executor.run(
                Step(2, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
                b'{}\n',
                None,
                allow_uncontained=False,
            )

    assert first.failure is None, step_transcript(
        first,
        stream.getvalue(),
        command=cast('tuple[str, ...]', backend.managed_steps[0].process.args),
    )
    assert raised.value.signum == signal.SIGTERM
    assert events.count('start') == 1


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (0, False),
        (1, False),
        (259, False),
        (0x7FFFFFFF, False),
        (0x80000000, False),
        (0x80000001, True),
        (0x80000002, True),
        (0x80000003, True),
        (0x80000004, True),
        (0x80000005, False),
        (0xBFFFFFFF, False),
        (0xC0000000, True),
        (0xC0000005, True),
        (0xCFFFFFFF, True),
        (0xD0000000, False),
        (0xDFFFFFFF, False),
        (0xE0000000, True),
        (0xE0434F4D, True),
        (0xE06D7363, True),
        (0xEFFFFFFF, True),
        (0xF0000000, False),
        (0xFFFFFFFF, False),
        (-1, False),
    ],
)
def test_windows_crash_classification_boundaries(value: int, expected: bool) -> None:
    assert _is_windows_crash(value) is expected
