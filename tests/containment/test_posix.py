import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from sequential_hooks._containment import ManagedStep, _posix
from sequential_hooks._containment._posix import PosixBackend, _PosixManagedStep
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._executor import ForwardedSignal, ProcessExecutor
from sequential_hooks._types import ContainmentState, FailureKind, Step, StepResult
from tests._helpers import (
    CHILD_TIMEOUT_SECONDS,
    PROCESS_GROUP_FLAGS,
    finish_process,
    step_transcript,
)

_DELAYED_EXEC_CODE = (
    'import os, sys, time; time.sleep(float(sys.argv[1])); os.execv(sys.argv[2], sys.argv[2:])'
)
_FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'hook_child.py'
_WATCHDOG_SECONDS = 3.0


class _SignalProcess:
    def __init__(
        self,
        returncode: int | None,
        *,
        wait_error: subprocess.TimeoutExpired | None = None,
    ) -> None:
        self.pid = 12345
        self.poll_calls = 0
        self.returncode = returncode
        self.wait_error = wait_error
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        self.poll_calls += 1
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        self.returncode = 0
        return self.returncode


def _defer_timeout_until_ready(
    monkeypatch: pytest.MonkeyPatch,
    ready_file: Path,
) -> list[float]:
    real_start = PosixBackend.start
    timeout_starts: list[float] = []

    def start_after_readiness(
        backend: PosixBackend,
        step: Step,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        managed = real_start(
            backend,
            step,
            allow_uncontained=allow_uncontained,
        )
        assert isinstance(managed, _PosixManagedStep)
        child_stdin = managed.process.stdin
        if child_stdin is None:
            managed.abort_before_release()
            raise AssertionError('ready fixture stdin was not created')
        try:
            child_stdin.write(b'{}\n')
            child_stdin.close()
        except Exception:
            managed.abort_before_release()
            raise
        deadline = time.monotonic() + 2.0
        while (
            not ready_file.exists()
            and managed.process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        if not ready_file.exists():
            managed.abort_before_release()
            raise AssertionError('fixture did not publish readiness within 2 seconds')
        managed.process.stdin = BytesIO()
        timeout_starts.append(time.monotonic())
        return managed

    monkeypatch.setattr(PosixBackend, 'start', start_after_readiness)
    return timeout_starts


def _delay_posix_child_start(
    monkeypatch: pytest.MonkeyPatch,
    delay_seconds: float,
) -> None:
    real_popen = subprocess.Popen

    def delayed_popen(
        *args: Any,  # noqa: ANN401 - Forward the overloaded `subprocess.Popen` boundary.
        **kwargs: Any,  # noqa: ANN401 - Forward the overloaded `subprocess.Popen` boundary.
    ) -> subprocess.Popen[bytes]:
        argv = cast('tuple[str, ...]', args[0])
        delayed_argv = (
            sys.executable,
            '-c',
            _DELAYED_EXEC_CODE,
            str(delay_seconds),
            *argv,
        )
        return cast(
            'subprocess.Popen[bytes]',
            real_popen(delayed_argv, *args[1:], **kwargs),
        )

    monkeypatch.setattr(_posix.subprocess, 'Popen', delayed_popen)


def _kill_recorded_pids(pid_file: Path) -> None:
    if sys.platform == 'win32':
        return
    if not pid_file.exists():
        return
    recorded = cast('dict[str, int] | int', json.loads(pid_file.read_text()))
    values = tuple(recorded.values()) if isinstance(recorded, dict) else (recorded,)
    for pid in values:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _published_file(path: Path) -> Path:
    return Path(f'{path}.published')


def _run_fixture(
    mode: str,
    *args: str,
    diagnostics: Diagnostics | None = None,
    timeout: float | None = CHILD_TIMEOUT_SECONDS,
) -> StepResult:
    return ProcessExecutor(diagnostics or Diagnostics(StringIO())).run(
        Step(1, (sys.executable, str(_FIXTURE), mode, *args)),
        b'{}\n',
        timeout,
        allow_uncontained=False,
    )


def _wait_for_missing(pids: list[int]) -> list[int]:
    deadline = time.monotonic() + 2.0
    while any(_pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    return [pid for pid in pids if _pid_exists(pid)]


def _watchdog_cleanup(
    pid_file: Path,
    completed: threading.Event,
    forced_cleanup: threading.Event,
    deadline: float | None = None,
) -> None:
    absolute_deadline = deadline if deadline is not None else time.monotonic() + _WATCHDOG_SECONDS
    while True:
        if completed.is_set():
            return
        remaining = absolute_deadline - time.monotonic()
        if remaining <= 0:
            break
        completed.wait(min(0.01, remaining))
    forced_cleanup.set()
    _kill_recorded_pids(pid_file)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_backend_preserves_literal_argv_and_disables_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    real_popen = subprocess.Popen
    popen_args: tuple[Any, ...] | None = None
    popen_kwargs: dict[str, Any] = {}

    def recording_popen(
        *args: Any,  # noqa: ANN401 - Forward the overloaded `subprocess.Popen` boundary.
        **kwargs: Any,  # noqa: ANN401 - Forward the overloaded `subprocess.Popen` boundary.
    ) -> subprocess.Popen[bytes]:
        nonlocal popen_args
        popen_args = args
        popen_kwargs.update(kwargs)
        return cast('subprocess.Popen[bytes]', real_popen(*args, **kwargs))

    monkeypatch.setattr(_posix.subprocess, 'Popen', recording_popen)
    argv = ('space value', '*', '$HOME', '`uname`', '; touch nope', '--check')

    result = _run_fixture('show-argv', *argv, diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'show-argv', *argv)
    )

    assert result.failure is None, result_transcript
    assert json.loads(result.stdout) == list(argv), result_transcript
    assert popen_args is not None, result_transcript
    assert popen_args[0] == (
        sys.executable,
        str(_FIXTURE),
        'show-argv',
        *argv,
    ), result_transcript
    assert popen_kwargs['creationflags'] == 0, result_transcript
    assert popen_kwargs['shell'] is False, result_transcript
    assert popen_kwargs['start_new_session'] is True, result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_backend_starts_step_in_new_session() -> None:
    stream = StringIO()
    result = _run_fixture('report-process', diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'report-process')
    )

    assert result.failure is None, result_transcript
    process_ids = json.loads(result.stdout)
    assert result.containment is ContainmentState.CONTAINED, result_transcript
    assert process_ids['pid'] == process_ids['pgid'] == process_ids['sid'], result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_group_waits_for_exiting_leader_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(None)
    attempts = 0

    def exiting_then_signalable(_pgid: int, _signum: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError('zombie-only group')
        assert process.poll_calls == 1
        assert process.wait_timeouts == [8.0]

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(_posix.os, 'killpg', exiting_then_signalable)
    monkeypatch.setattr(_posix.time, 'monotonic', lambda: 12.0)

    managed._signal_group(signal.SIGTERM, 20.0)

    assert process.poll_calls == 1
    assert process.wait_timeouts == [8.0]
    assert attempts == 2


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_group_preserves_exact_group_and_signal_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(0)
    calls: list[tuple[int, int]] = []

    def record_signal(pgid: int, signum: int) -> None:
        calls.append((pgid, signum))
        if len(calls) == 1:
            raise PermissionError('zombie-only group')
        assert process.poll_calls == 1

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(_posix.os, 'killpg', record_signal)

    managed._signal_group(signal.SIGKILL, 20.0)

    assert calls == [(12345, signal.SIGKILL), (12345, signal.SIGKILL)]


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_group_preserves_permission_denial_after_reap_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(
        None,
        wait_error=subprocess.TimeoutExpired('exiting-child', 0.0),
    )
    error = PermissionError('live group denied')
    attempts = 0

    def deny_signal(_pgid: int, _signum: int) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(_posix.os, 'killpg', deny_signal)
    monkeypatch.setattr(_posix.time, 'monotonic', lambda: 20.0)

    with pytest.raises(PermissionError) as raised:
        managed._signal_group(signal.SIGTERM, 20.0)

    assert raised.value is error
    assert process.poll_calls == 1
    assert process.wait_timeouts == [0.0]
    assert attempts == 1


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_group_propagates_repeated_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(0)
    errors = iter(
        (
            PermissionError('zombie-only group'),
            PermissionError('surviving group denied'),
        )
    )

    def deny_signal(_pgid: int, _signum: int) -> None:
        raise next(errors)

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(_posix.os, 'killpg', deny_signal)

    with pytest.raises(PermissionError, match='surviving group denied'):
        managed._signal_group(signal.SIGTERM, 20.0)

    assert process.poll_calls == 1


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_group_suppresses_vanished_group_after_reaping_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(0)
    errors = iter((PermissionError('zombie-only group'), ProcessLookupError('vanished')))
    attempts = 0

    def deny_then_vanish(_pgid: int, _signum: int) -> None:
        nonlocal attempts
        attempts += 1
        raise next(errors)

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(_posix.os, 'killpg', deny_then_vanish)

    managed._signal_group(signal.SIGTERM, 20.0)

    assert process.poll_calls == 1
    assert attempts == 2


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_forward_signal_bounds_exiting_leader_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    process = _SignalProcess(
        None,
        wait_error=subprocess.TimeoutExpired('exiting-child', 0.25),
    )
    error = PermissionError('exiting group denied')
    monotonic_values = iter((10.0, 10.25))

    def deny_signal(_pgid: int, _signum: int) -> None:
        raise error

    monkeypatch.setattr(managed, '_process', process, raising=False)
    monkeypatch.setattr(managed, '_graceful_signum', None, raising=False)
    monkeypatch.setattr(_posix, '_FORCE_KILL_WAIT_SECONDS', 0.5)
    monkeypatch.setattr(_posix.os, 'killpg', deny_signal)
    monkeypatch.setattr(_posix.time, 'monotonic', lambda: next(monotonic_values))

    with pytest.raises(PermissionError) as raised:
        managed.forward_signal(signal.SIGINT)

    assert raised.value is error
    assert managed._graceful_signum == signal.SIGINT
    assert process.wait_timeouts == [0.25]


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_terminate_tree_shares_each_phase_deadline_with_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    events: list[tuple[str, float] | tuple[str, int, float]] = []
    group_results = iter((False, True))
    monotonic_values = iter((10.0, 20.0))

    def monotonic() -> float:
        value = next(monotonic_values)
        events.append(('clock', value))
        return value

    def signal_group(signum: int, deadline: float) -> None:
        events.append(('signal', signum, deadline))

    def wait_for_group_exit(deadline: float) -> bool:
        events.append(('group', deadline))
        return next(group_results)

    def wait_for_process_reap(deadline: float) -> bool:
        events.append(('reap', deadline))
        return True

    monkeypatch.setattr(managed, '_graceful_signum', None, raising=False)
    monkeypatch.setattr(managed, '_signal_group', signal_group, raising=False)
    monkeypatch.setattr(managed, '_wait_for_group_exit', wait_for_group_exit, raising=False)
    monkeypatch.setattr(managed, '_wait_for_process_reap', wait_for_process_reap, raising=False)
    monkeypatch.setattr(_posix, '_FORCE_KILL_WAIT_SECONDS', 0.5)
    monkeypatch.setattr(_posix.time, 'monotonic', monotonic)

    managed.terminate_tree(2.0)

    assert events == [
        ('clock', 10.0),
        ('signal', signal.SIGTERM, 12.0),
        ('group', 12.0),
        ('clock', 20.0),
        ('signal', signal.SIGKILL, 20.5),
        ('group', 20.5),
        ('reap', 20.5),
    ]


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_executor_cleans_group_after_leader_exits(tmp_path: Path) -> None:
    stream = StringIO()
    pid_file = tmp_path / 'leader-exits-pids.json'
    completed = threading.Event()
    forced_cleanup = threading.Event()
    watchdog = threading.Thread(
        target=_watchdog_cleanup,
        args=(
            pid_file,
            completed,
            forced_cleanup,
            time.monotonic() + _WATCHDOG_SECONDS,
        ),
        daemon=True,
    )
    watchdog.start()

    try:
        result = _run_fixture(
            'child-tree-exit', '10', str(pid_file), timeout=None, diagnostics=Diagnostics(stream)
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), 'child-tree-exit', '10', str(pid_file)),
        )
        pids = [int(value) for value in json.loads(pid_file.read_text()).values()]
        remaining = [pid for pid in pids if _pid_exists(pid)]
    finally:
        completed.set()
        watchdog.join(0.5)
        _kill_recorded_pids(pid_file)

    assert not watchdog.is_alive(), result_transcript
    assert not forced_cleanup.is_set(), result_transcript
    assert result.failure is None, result_transcript
    assert result.returncode == 0, result_transcript
    assert remaining == [], result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_executor_timeout_leaves_no_original_group_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = StringIO()
    pid_file = tmp_path / 'timeout-pids.json'
    _delay_posix_child_start(monkeypatch, 0.6)
    timeout_starts = _defer_timeout_until_ready(
        monkeypatch,
        _published_file(pid_file),
    )
    completed = threading.Event()
    forced_cleanup = threading.Event()
    watchdog = threading.Thread(
        target=_watchdog_cleanup,
        args=(
            pid_file,
            completed,
            forced_cleanup,
            time.monotonic() + _WATCHDOG_SECONDS,
        ),
        daemon=True,
    )
    watchdog.start()

    try:
        result = _run_fixture(
            'child-tree', '10', str(pid_file), timeout=0.5, diagnostics=Diagnostics(stream)
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), 'child-tree', '10', str(pid_file)),
        )
        pids = [int(value) for value in json.loads(pid_file.read_text()).values()]
        remaining = [pid for pid in pids if _pid_exists(pid)]
    finally:
        completed.set()
        watchdog.join(0.5)
        _kill_recorded_pids(pid_file)

    assert not watchdog.is_alive(), result_transcript
    assert not forced_cleanup.is_set(), result_transcript
    assert len(timeout_starts) == 1, result_transcript
    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.TIMEOUT, result_transcript
    assert remaining == [], result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_posix_force_kill_reap_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    release_wait = threading.Event()
    completed = threading.Event()
    errors: list[RuntimeError] = []

    class NeverReapedProcess:
        pid = 12345
        returncode: int | None = None

        def poll(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired('never-reaped', timeout)
            release_wait.wait(1.0)
            return 1

    monkeypatch.setattr(_posix, '_FORCE_KILL_WAIT_SECONDS', 0.05, raising=False)
    monkeypatch.setattr(managed, '_process', NeverReapedProcess(), raising=False)
    monkeypatch.setattr(managed, '_graceful_signum', None, raising=False)
    monkeypatch.setattr(managed, '_group_exists', lambda: True, raising=False)

    def ignore_signal(_signum: int, _deadline: float) -> None:
        pass

    monkeypatch.setattr(managed, '_signal_group', ignore_signal, raising=False)

    def terminate() -> None:
        try:
            managed.terminate_tree(0.0)
        except RuntimeError as error:
            errors.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=terminate, daemon=True)
    worker.start()
    completed_within_bound = completed.wait(0.3)
    release_wait.set()
    worker.join(0.5)

    assert completed_within_bound
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert 'did not disappear' in str(errors[0])


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_posix_force_kill_waits_for_exact_group_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    managed = _PosixManagedStep.__new__(_PosixManagedStep)
    group_states = iter((True, True, False))
    group_probes: list[bool] = []
    signals: list[int] = []

    class ReapedProcess:
        pid = 12345
        returncode = 1

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    def group_exists() -> bool:
        state = next(group_states)
        group_probes.append(state)
        return state

    monkeypatch.setattr(managed, '_process', ReapedProcess(), raising=False)
    monkeypatch.setattr(managed, '_graceful_signum', None, raising=False)
    monkeypatch.setattr(managed, '_group_exists', group_exists, raising=False)

    def record_signal(signum: int, _deadline: float) -> None:
        signals.append(signum)

    monkeypatch.setattr(managed, '_signal_group', record_signal, raising=False)

    managed.terminate_tree(0.0)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert group_probes == [True, True, False]


@pytest.mark.parametrize(
    'mode',
    ['child-tree-setpgid', 'child-tree-setsid'],
)
@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_process_leaving_original_group_is_an_explicit_exclusion(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = StringIO()
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    pid_file = tmp_path / f'{mode}-pids.json'
    _delay_posix_child_start(monkeypatch, 0.6)
    timeout_starts = _defer_timeout_until_ready(
        monkeypatch,
        _published_file(pid_file),
    )
    completed = threading.Event()
    forced_cleanup = threading.Event()
    watchdog = threading.Thread(
        target=_watchdog_cleanup,
        args=(
            pid_file,
            completed,
            forced_cleanup,
            time.monotonic() + _WATCHDOG_SECONDS,
        ),
        daemon=True,
    )
    watchdog.start()

    try:
        result = _run_fixture(
            mode, '10', str(pid_file), timeout=0.5, diagnostics=Diagnostics(stream)
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), mode, '10', str(pid_file)),
        )
        pids = json.loads(pid_file.read_text())
        escaped_pid = int(pids['escaped'])
        assert result.failure is not None, result_transcript
        assert result.failure.kind is FailureKind.TIMEOUT, result_transcript
        assert _pid_exists(escaped_pid), result_transcript
        os.kill(escaped_pid, signal.SIGKILL)
        assert _wait_for_missing([escaped_pid]) == [], result_transcript
    finally:
        completed.set()
        watchdog.join(0.5)
        _kill_recorded_pids(pid_file)

    assert not watchdog.is_alive(), result_transcript
    assert not forced_cleanup.is_set(), result_transcript
    assert len(timeout_starts) == 1, result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_signal_context_forwards_actual_signal_and_cleans_child(tmp_path: Path) -> None:
    signal_file = tmp_path / 'signal.json'
    ready_file = tmp_path / 'signal.ready'
    executor = ProcessExecutor(Diagnostics(StringIO()))
    previous_handler = signal.getsignal(signal.SIGINT)

    def send_signal() -> None:
        deadline = time.monotonic() + 2.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGINT if ready_file.exists() else signal.SIGTERM)

    sender = threading.Thread(target=send_signal)
    with executor.signal_forwarding():
        sender.start()
        with pytest.raises(ForwardedSignal) as raised:
            executor.run(
                Step(
                    1,
                    (
                        sys.executable,
                        str(_FIXTURE),
                        'record-signal',
                        str(signal_file),
                        str(ready_file),
                        '10',
                    ),
                ),
                b'{}\n',
                None,
                allow_uncontained=False,
            )
    sender.join()

    recorded = json.loads(signal_file.read_text())
    assert raised.value.signum == signal.SIGINT
    assert recorded['signum'] == signal.SIGINT
    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert not _pid_exists(int(recorded['pid']))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_timeout_escalates_after_one_grace_period(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = StringIO()
    if sys.platform == 'win32':
        pytest.skip('POSIX containment requires POSIX')
    pid_file = tmp_path / 'ignore-term.pid'
    _delay_posix_child_start(monkeypatch, 0.1)
    timeout_starts = _defer_timeout_until_ready(
        monkeypatch,
        _published_file(pid_file),
    )
    completed = threading.Event()
    forced_cleanup = threading.Event()
    watchdog = threading.Thread(
        target=_watchdog_cleanup,
        args=(
            pid_file,
            completed,
            forced_cleanup,
            time.monotonic() + 3.0,
        ),
        daemon=True,
    )
    watchdog.start()

    try:
        result = _run_fixture(
            'sleep-ignore-term', '10', str(pid_file), timeout=0.05, diagnostics=Diagnostics(stream)
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), 'sleep-ignore-term', '10', str(pid_file)),
        )
        elapsed = time.monotonic() - timeout_starts[0]
        pid = int(pid_file.read_text())
        assert result.failure is not None, result_transcript
        assert result.failure.kind is FailureKind.TIMEOUT, result_transcript
        assert result.returncode == -signal.SIGKILL, result_transcript
        assert 1.0 <= elapsed < 2.0, result_transcript
        assert len(timeout_starts) == 1, result_transcript
        assert not _pid_exists(pid), result_transcript
    finally:
        completed.set()
        watchdog.join(0.5)
        _kill_recorded_pids(pid_file)

    assert not watchdog.is_alive(), result_transcript
    assert not forced_cleanup.is_set(), result_transcript


@pytest.mark.skipif(os.name != 'posix', reason='POSIX containment requires POSIX')
def test_timeout_never_signals_unrelated_pid(tmp_path: Path) -> None:
    stream = StringIO()
    unrelated_pid_file = tmp_path / 'unrelated.pid'
    unrelated = subprocess.Popen(
        [
            sys.executable,
            str(_FIXTURE),
            'sleep-ignore-term',
            '10',
            str(unrelated_pid_file),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    unrelated_ready_file = _published_file(unrelated_pid_file)
    deadline = time.monotonic() + 2.0
    while not unrelated_ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.005)

    try:
        assert unrelated_ready_file.exists()
        result = _run_fixture('sleep', '10', timeout=0.05, diagnostics=Diagnostics(stream))
        result_transcript = step_transcript(
            result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'sleep', '10')
        )

        assert result.failure is not None, result_transcript
        assert result.failure.kind is FailureKind.TIMEOUT, result_transcript
        assert unrelated_pid_file.read_text() == str(unrelated.pid), result_transcript
        assert unrelated.poll() is None, result_transcript
    finally:
        unrelated.kill()
        finish_process(unrelated, timeout=10)


# POSIX cannot clean descendants after an uncatchable wrapper death. No passing
# test simulates or claims that excluded cross-platform guarantee.
