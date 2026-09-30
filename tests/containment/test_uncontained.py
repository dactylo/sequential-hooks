import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from sequential_hooks._containment import ContainmentError, _uncontained
from sequential_hooks._containment._uncontained import (
    UncontainedBackend,
    _UncontainedManagedStep,
)
from sequential_hooks._types import ContainmentState, Step

if TYPE_CHECKING:
    from sequential_hooks._containment._windows_api import WindowsApi

_FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'hook_child.py'
_STEP = Step(1, (sys.executable, '-c', 'pass'))


class _FakeWindowsApi:
    def __init__(
        self,
        events: list[str],
        *,
        ctrl_break_error: OSError | None = None,
        ctrl_break_result: bool = True,
    ) -> None:
        self.ctrl_break_error = ctrl_break_error
        self.ctrl_break_result = ctrl_break_result
        self.events = events
        self.wait_results = [True]
        self.wait_error: OSError | None = None
        self.waits: list[tuple[subprocess.Popen[bytes], float]] = []

    def generate_ctrl_break(self, process_group_id: int) -> bool:
        self.events.append(f'ctrl-break:{process_group_id}')
        if self.ctrl_break_error is not None:
            raise self.ctrl_break_error
        return self.ctrl_break_result

    def wait_process(self, process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        self.waits.append((process, timeout_seconds))
        if self.wait_error is not None:
            raise self.wait_error
        return self.wait_results.pop(0)


class _UncontainedProcess:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.pid = 5150
        self.returncode: int | None = None
        self.stdin = BytesIO()
        self.stdout = BytesIO()
        self.stderr = BytesIO()

    def kill(self) -> None:
        self.events.append('kill')
        self.returncode = 1

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        raise AssertionError(f'raw signal {signum} reached the Windows child')

    def terminate(self) -> None:
        self.events.append('terminate')
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        self.events.append('wait')
        if self.returncode is None:
            raise subprocess.TimeoutExpired('uncontained', timeout or 0.0)
        return self.returncode


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _start_uncontained_windows(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeWindowsApi,
) -> tuple[_UncontainedManagedStep, _UncontainedProcess, list[tuple[bool, int]]]:
    process = _UncontainedProcess()
    starts: list[tuple[bool, int]] = []

    def record_start(
        step: Step,
        *,
        contained: bool,
        creationflags: int = 0,
    ) -> subprocess.Popen[bytes]:
        assert step is _STEP
        starts.append((contained, creationflags))
        return cast('subprocess.Popen[bytes]', process)

    monkeypatch.setattr(_uncontained, 'start_process', record_start)
    managed = _UncontainedManagedStep(
        _STEP,
        platform_name='win32',
        windows_api=cast('WindowsApi', api),
    )
    return managed, process, starts


def test_uncontained_backend_reports_reduced_state() -> None:
    backend = UncontainedBackend()
    step = Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin'))

    managed = backend.start(step, allow_uncontained=True)
    try:
        managed.release()
        managed.stdin.close()
        assert managed.process.wait(timeout=2.0) == 0
        assert managed.containment is ContainmentState.UNCONTAINED
    finally:
        managed.finish()


def test_uncontained_backend_requires_explicit_override() -> None:
    backend = UncontainedBackend()
    step = Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin'))

    with pytest.raises(ContainmentError, match='explicit override'):
        backend.start(step, allow_uncontained=False)


def test_uncontained_force_kill_reap_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _UncontainedManagedStep.__new__(_UncontainedManagedStep)
    release_wait = threading.Event()
    completed = threading.Event()
    errors: list[RuntimeError] = []

    class NeverReapedProcess:
        returncode: int | None = None

        def kill(self) -> None:
            pass

        def poll(self) -> None:
            pass

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired('never-reaped', timeout)
            release_wait.wait(1.0)
            return 1

    monkeypatch.setattr(_uncontained, '_FORCE_KILL_WAIT_SECONDS', 0.05, raising=False)
    monkeypatch.setattr(managed, '_process', NeverReapedProcess(), raising=False)
    monkeypatch.setattr(managed, '_windows_api', None, raising=False)
    monkeypatch.setattr(managed, '_graceful_signal_forwarded', False, raising=False)

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
    assert 'could not be reaped' in str(errors[0])


@pytest.mark.parametrize('finish', [False, True])
def test_uncontained_windows_cached_exit_still_waits_for_native_completion(
    monkeypatch: pytest.MonkeyPatch,
    finish: bool,
) -> None:
    api = _FakeWindowsApi([])
    api.wait_results = [False, True]
    managed, process, _starts = _start_uncontained_windows(monkeypatch, api)
    process.returncode = 1

    try:
        if finish:
            managed.finish()
        else:
            managed.terminate_tree(0.0)
        assert len(api.waits) == 2
        assert all(owner is process for owner, _timeout in api.waits)
        assert 0.0 <= api.waits[0][1] <= (1.0 if finish else 0.0)
        assert 0.0 < api.waits[1][1] <= 1.0
        assert process.events == ['wait']
    finally:
        _close_process_pipes(cast('subprocess.Popen[bytes]', process))


@pytest.mark.parametrize(
    ('ctrl_break_result', 'ctrl_break_error'),
    [
        (False, None),
        (True, OSError('Ctrl+Break refused')),
    ],
)
def test_uncontained_windows_failed_signal_keeps_forced_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    ctrl_break_result: bool,
    ctrl_break_error: OSError | None,
) -> None:
    api = _FakeWindowsApi(
        [],
        ctrl_break_error=ctrl_break_error,
        ctrl_break_result=ctrl_break_result,
    )
    managed, process, _starts = _start_uncontained_windows(monkeypatch, api)

    try:
        managed.forward_signal(2)
        managed.forward_signal(15)
        managed.terminate_tree(0.0)

        assert api.events == [f'ctrl-break:{process.pid}', f'ctrl-break:{process.pid}']
        assert process.events == ['terminate', 'wait']
        assert api.waits == [(process, 0.0)]
    finally:
        _close_process_pipes(cast('subprocess.Popen[bytes]', process))


@pytest.mark.parametrize('graceful_completion', [False, True])
def test_uncontained_windows_grace_and_force_waits_use_remaining_deadlines(
    monkeypatch: pytest.MonkeyPatch,
    graceful_completion: bool,
) -> None:
    api = _FakeWindowsApi([])
    managed, process, _starts = _start_uncontained_windows(monkeypatch, api)
    now = 10.0
    popen_waits: list[float | None] = []

    def monotonic() -> float:
        return now

    def native_wait(owner: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        nonlocal now
        api.waits.append((owner, timeout_seconds))
        if len(api.waits) == 1:
            now += 0.5
            if graceful_completion:
                process.returncode = 0
            return graceful_completion
        now += 0.75
        return True

    def wait(timeout: float | None = None) -> int:
        popen_waits.append(timeout)
        assert process.returncode is not None
        return process.returncode

    monkeypatch.setattr(_uncontained.time, 'monotonic', monotonic)
    monkeypatch.setattr(api, 'wait_process', native_wait)
    monkeypatch.setattr(process, 'wait', wait)
    try:
        managed.forward_signal(2)
        managed.terminate_tree(0.5)
        assert api.waits == (
            [(process, 0.5)] if graceful_completion else [(process, 0.5), (process, 1.0)]
        )
        assert process.events == ([] if graceful_completion else ['kill'])
        assert popen_waits == ([0.0] if graceful_completion else [0.25])
    finally:
        _close_process_pipes(cast('subprocess.Popen[bytes]', process))


@pytest.mark.parametrize('signum', [1, 2, 15, 21])
def test_uncontained_windows_maps_every_signal_to_child_ctrl_break(
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    api = _FakeWindowsApi([])
    managed, process, starts = _start_uncontained_windows(monkeypatch, api)

    try:
        managed.forward_signal(signum)
        managed.forward_signal(signum + 100)

        assert starts == [
            (
                False,
                cast('int', getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)),
            )
        ]
        assert api.events == [f'ctrl-break:{process.pid}']
        process.returncode = 0
    finally:
        managed.finish()
        _close_process_pipes(cast('subprocess.Popen[bytes]', process))


@pytest.mark.parametrize('cached_exit', [False, True])
@pytest.mark.parametrize('wait_error', [None, OSError('native wait failed')])
def test_uncontained_windows_native_wait_failure_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
    cached_exit: bool,
    wait_error: OSError | None,
) -> None:
    api = _FakeWindowsApi([])
    api.wait_results = [False, False]
    api.wait_error = wait_error
    managed, process, _starts = _start_uncontained_windows(monkeypatch, api)
    if cached_exit:
        process.returncode = 1

    try:
        with pytest.raises(ContainmentError, match='could not be reaped') as raised:
            managed.terminate_tree(0.0)
        if wait_error is not None:
            assert raised.value.__cause__ is wait_error
        assert process.events == ([] if cached_exit else ['terminate'])
        assert len(api.waits) == (1 if wait_error is not None else 2)
    finally:
        _close_process_pipes(cast('subprocess.Popen[bytes]', process))
