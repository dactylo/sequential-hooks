import contextlib
import ctypes
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from sequential_hooks import _executable as executable_module
from sequential_hooks import _executor as executor_module
from sequential_hooks._containment import ContainmentError, ManagedStep
from sequential_hooks._containment._uncontained import _UncontainedManagedStep
from sequential_hooks._containment._windows import WindowsBackend, _WindowsManagedStep
from sequential_hooks._containment._windows_api import NativeWindowsApi
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._executable import ExecutableStatus, resolve_executable
from sequential_hooks._executor import (
    _STDOUT_LIMIT_BYTES,
    ForwardedSignal,
    ProcessExecutor,
)
from sequential_hooks._types import ContainmentState, FailureKind, Step, StepResult
from tests._helpers import (
    CHILD_TIMEOUT_SECONDS,
    PROCESS_GROUP_FLAGS,
    finish_process,
    process_transcript,
    run_setup,
    step_transcript,
)

globals()['pytestmark'] = pytest.mark.skipif(
    sys.platform != 'win32' or platform.python_implementation() != 'CPython',
    reason='native Windows containment requires native Windows CPython',
)

_DELAYED_FIXTURE_CODE = (
    'import runpy, sys, time; time.sleep(float(sys.argv[1])); '
    "sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')"
)
_ERROR_INVALID_PARAMETER = 87
_FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'hook_child.py'
_PROCESS_SYNCHRONIZE = 0x00100000
_READINESS_WATCHDOG_SECONDS = 10.0
_SOURCE_ROOT = Path(__file__).parents[2] / 'src'
_UNSIGNED_EXIT_VALUES = (
    0,
    1,
    259,
    0x7FFFFFFF,
    0x80000000,
    0x80000001,
    0x80000002,
    0x80000003,
    0x80000004,
    0x80000005,
    0xBFFFFFFF,
    0xC0000000,
    0xC0000005,
    0xCFFFFFFF,
    0xD0000000,
    0xDFFFFFFF,
    0xE0000000,
    0xE0434F4D,
    0xE06D7363,
    0xEFFFFFFF,
    0xF0000000,
    0xFFFFFFFF,
)
_WAIT_FAILED = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102
_WINDOWS_CREATE_NO_WINDOW = cast('int', getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
_WINDOWS_DO_NOT_INHERIT_HANDLE = False
_WINDOWS_SIGBREAK = cast('int', getattr(signal, 'SIGBREAK', 21))


class _ReadinessClock:
    def __init__(self, published: Path) -> None:
        self._real_monotonic = time.monotonic
        self._initial = self._real_monotonic()
        self._published = published
        self._advancing_since: float | None = None
        self.readiness_timed_out = False

    def monotonic(self) -> float:
        now = self._real_monotonic()
        if self._advancing_since is None:
            if now - self._initial >= _READINESS_WATCHDOG_SECONDS:
                self.readiness_timed_out = True
                # Expire the executor deadline and keep cleanup time advancing.
                self._advancing_since = self._initial
            elif self._published.exists():
                self._advancing_since = now
            else:
                return self._initial
        return self._initial + (now - self._advancing_since)


class _RecordingNativeApi:
    def __init__(
        self,
        *,
        ctrl_break_error: OSError | None = None,
        ctrl_break_result: bool | None = None,
        refuse_assignment: bool = False,
        refuse_creation: bool = False,
    ) -> None:
        self._native = NativeWindowsApi()
        self.ctrl_break_error = ctrl_break_error
        self.ctrl_break_process_groups: list[int] = []
        self.ctrl_break_result = ctrl_break_result
        self.ctrl_break_results: list[bool] = []
        self.events: list[str] = []
        self.refuse_assignment = refuse_assignment
        self.refuse_creation = refuse_creation

    def assign_pid(self, job_handle: int, pid: int) -> None:
        self.events.append('assign')
        if self.refuse_assignment:
            raise OSError('native assignment refused by test')
        self._native.assign_pid(job_handle, pid)

    def close_handle(self, handle: int) -> None:
        self.events.append('close')
        self._native.close_handle(handle)

    def create_kill_on_close_job(self) -> int:
        self.events.append('create')
        if self.refuse_creation:
            raise OSError('native job creation refused by test')
        return self._native.create_kill_on_close_job()

    def generate_ctrl_break(self, process_group_id: int) -> bool:
        self.events.append('ctrl-break')
        self.ctrl_break_process_groups.append(process_group_id)
        if self.ctrl_break_error is not None:
            raise self.ctrl_break_error
        result = (
            self._native.generate_ctrl_break(process_group_id)
            if self.ctrl_break_result is None
            else self.ctrl_break_result
        )
        self.ctrl_break_results.append(result)
        return result

    def terminate_job(self, job_handle: int) -> None:
        self.events.append('terminate')
        self._native.terminate_job(job_handle)

    def wait_job(self, job_handle: int, timeout_seconds: float) -> bool:
        return self._native.wait_job(job_handle, timeout_seconds)

    def wait_process(self, process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        return self._native.wait_process(process, timeout_seconds)


def _close_managed_pipes(managed: ManagedStep) -> None:
    for stream in (managed.stdin, managed.stdout, managed.stderr):
        stream.close()


def _last_windows_error_code() -> int:
    get_last_error = cast('Callable[[], int]', vars(ctypes)['get_last_error'])
    return get_last_error()


def _run_delayed_fixture(
    mode: str,
    *args: str,
    diagnostics: Diagnostics | None = None,
    startup_delay: float,
    timeout: float,
) -> StepResult:
    return ProcessExecutor(diagnostics or Diagnostics(StringIO())).run(
        Step(
            1,
            (
                sys.executable,
                '-c',
                _DELAYED_FIXTURE_CODE,
                str(startup_delay),
                str(_FIXTURE),
                mode,
                *args,
            ),
        ),
        b'{}\n',
        timeout,
        allow_uncontained=False,
    )


def _run_fixture(
    mode: str,
    *args: str,
    diagnostics: Diagnostics | None = None,
    input_bytes: bytes = b'{}\n',
    timeout: float | None = CHILD_TIMEOUT_SECONDS,
) -> StepResult:
    return ProcessExecutor(diagnostics or Diagnostics(StringIO())).run(
        Step(1, (sys.executable, str(_FIXTURE), mode, *args)),
        input_bytes,
        timeout,
        allow_uncontained=False,
    )


def _windows_error(error_code: int) -> OSError:
    win_error = cast('Callable[[int], OSError]', vars(ctypes)['WinError'])
    return win_error(error_code)


def _pid_is_running(pid: int) -> bool:
    win_dll = cast('Callable[..., Any]', vars(ctypes)['WinDLL'])
    kernel32: Any = win_dll('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(
        _PROCESS_SYNCHRONIZE,
        _WINDOWS_DO_NOT_INHERIT_HANDLE,
        pid,
    )
    if not process:
        error_code = _last_windows_error_code()
        if error_code == _ERROR_INVALID_PARAMETER:
            return False
        raise _windows_error(error_code)
    try:
        wait_result = kernel32.WaitForSingleObject(process, 0)
        if wait_result == _WAIT_TIMEOUT:
            return True
        if wait_result == _WAIT_OBJECT_0:
            return False
        if wait_result == _WAIT_FAILED:
            raise _windows_error(_last_windows_error_code())
        raise OSError(f'unexpected WaitForSingleObject result: {wait_result}')
    finally:
        if not kernel32.CloseHandle(process):
            raise _windows_error(_last_windows_error_code())


@pytest.fixture
def recorded_tree(tmp_path: Path) -> Iterator[Path]:
    record = tmp_path / 'tree.json'
    try:
        yield record
    finally:
        if record.exists():
            recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))
            with contextlib.ExitStack() as cleanup:
                for pid in recorded.values():
                    if _pid_is_running(pid):
                        cleanup.callback(
                            run_setup,
                            ['taskkill', '/F', '/T', '/PID', str(pid)],
                            check=False,
                            timeout=5,
                        )
            assert all(not _pid_is_running(pid) for pid in recorded.values()), recorded


def test_native_backend_assigns_launcher_before_release() -> None:
    api = _RecordingNativeApi()
    managed = WindowsBackend(api).start(
        Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
        allow_uncontained=False,
    )

    assert api.events == ['create', 'assign']
    managed.release()
    managed.process.wait(timeout=3.0)
    managed.finish()
    _close_managed_pipes(managed)

    assert api.events.index('assign') < api.events.index('terminate')


def test_native_console_delivery_then_forces_job_cleanup() -> None:
    api = _RecordingNativeApi()
    managed = WindowsBackend(api).start(
        Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
        allow_uncontained=False,
    )
    managed.release()
    managed.stdin.close()

    managed.forward_signal(_WINDOWS_SIGBREAK)
    managed.terminate_tree(0.0)
    managed.finish()
    _close_managed_pipes(managed)

    assert 'ctrl-break' in api.events
    assert api.ctrl_break_results == [True]
    assert 'terminate' in api.events


def test_native_job_closes_inherited_output_after_leader_exit(recorded_tree: Path) -> None:
    stream = StringIO()
    record = recorded_tree
    started = time.monotonic()
    result = _run_fixture('child-tree-exit', '10', str(record), diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'child-tree-exit', '10', str(record)),
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result_transcript
    assert elapsed < 3.0, result_transcript
    assert result.containment is ContainmentState.CONTAINED, result_transcript
    recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))
    assert not _pid_is_running(recorded['parent']), result_transcript
    assert not _pid_is_running(recorded['child']), result_transcript


def test_native_job_terminates_descendants_on_bounded_stop_output_limit(
    recorded_tree: Path,
) -> None:
    stream = StringIO()
    record = recorded_tree
    started = time.monotonic()
    result = _run_fixture(
        'child-tree-output',
        '10',
        str(record),
        str(_STDOUT_LIMIT_BYTES + 1),
        timeout=10.0,
        diagnostics=Diagnostics(stream),
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(
            sys.executable,
            str(_FIXTURE),
            'child-tree-output',
            '10',
            str(record),
            str(_STDOUT_LIMIT_BYTES + 1),
        ),
    )
    elapsed = time.monotonic() - started
    assert elapsed < 15.0, result_transcript
    assert Path(f'{record}.published').exists(), result_transcript
    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.OUTPUT_LIMIT, result_transcript
    assert result.containment is ContainmentState.CONTAINED, result_transcript
    recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))
    assert not _pid_is_running(recorded['parent']), result_transcript
    assert not _pid_is_running(recorded['child']), result_transcript


@pytest.mark.parametrize('startup_delay', [0.0, 0.6])
def test_native_job_terminates_descendants_on_bounded_stop_timeout(
    recorded_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    startup_delay: float,
) -> None:
    stream = StringIO()
    record = recorded_tree
    published = Path(f'{record}.published')
    clock = _ReadinessClock(published)
    monkeypatch.setattr(executor_module, 'time', clock)
    started = time.monotonic()
    if startup_delay:
        result = _run_delayed_fixture(
            'child-tree',
            '10',
            str(record),
            startup_delay=startup_delay,
            timeout=0.2,
            diagnostics=Diagnostics(stream),
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(
                sys.executable,
                '-c',
                _DELAYED_FIXTURE_CODE,
                str(startup_delay),
                str(_FIXTURE),
                'child-tree',
                '10',
                str(record),
            ),
        )
    else:
        result = _run_fixture(
            'child-tree', '10', str(record), timeout=0.2, diagnostics=Diagnostics(stream)
        )
        result_transcript = step_transcript(
            result,
            stream.getvalue(),
            command=(sys.executable, str(_FIXTURE), 'child-tree', '10', str(record)),
        )
    elapsed = time.monotonic() - started
    assert elapsed < 15.0, result_transcript
    assert not clock.readiness_timed_out, result_transcript
    assert published.exists(), result_transcript
    assert result.failure is not None, result_transcript
    assert result.failure.kind is FailureKind.TIMEOUT, result_transcript
    assert result.containment is ContainmentState.CONTAINED, result_transcript
    recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))
    assert not _pid_is_running(recorded['parent']), result_transcript
    assert not _pid_is_running(recorded['child']), result_transcript


def test_native_launcher_command_is_isolated_and_absolute() -> None:
    managed = cast(
        '_WindowsManagedStep',
        WindowsBackend().start(
            Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
            allow_uncontained=False,
        ),
    )
    command = cast('tuple[str, ...]', managed._raw_process.args)

    assert command[:3] == (sys.executable, '-I', '-S')
    assert Path(command[3]).is_absolute()
    assert '-m' not in command

    assert managed._dispose_setup_failure() is None


def test_native_launcher_preserves_spaces_non_ascii_paths_argv_and_lf(
    tmp_path: Path,
) -> None:
    stream = StringIO()
    fixture_dir = tmp_path / 'space and snowman-\u2603'
    fixture_dir.mkdir()
    copied_fixture = fixture_dir / 'hook child.py'
    shutil.copyfile(_FIXTURE, copied_fixture)
    expected = ['two words', 'snowman-\u2603', 'trailing\\']
    result = ProcessExecutor(Diagnostics(stream)).run(
        Step(1, (sys.executable, str(copied_fixture), 'show-argv', *expected)),
        b'{}\n',
        CHILD_TIMEOUT_SECONDS,
        allow_uncontained=False,
    )
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(copied_fixture), 'show-argv', *expected),
    )
    assert result.returncode == 0, result_transcript
    assert result.stdout.endswith(b'\n'), result_transcript
    assert b'\r\n' not in result.stdout, result_transcript
    assert json.loads(result.stdout) == expected, result_transcript


@pytest.mark.parametrize('value', _UNSIGNED_EXIT_VALUES)
def test_native_launcher_relays_every_unsigned_exit(value: int) -> None:
    stream = StringIO()
    result = _run_fixture('windows-exit', hex(value), diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result,
        stream.getvalue(),
        command=(sys.executable, str(_FIXTURE), 'windows-exit', hex(value)),
    )
    assert result.returncode == value, result_transcript


def test_native_launcher_transports_hook_payload_and_output() -> None:
    stream = StringIO()
    payload = b'{"native":"payload"}\n'
    result = _run_fixture('echo-input', input_bytes=payload, diagnostics=Diagnostics(stream))
    result_transcript = step_transcript(
        result, stream.getvalue(), command=(sys.executable, str(_FIXTURE), 'echo-input')
    )

    assert result.returncode == 0, result_transcript
    assert result.stdout == payload, result_transcript


def test_native_no_console_ctrl_break_failure_still_cleans_job(
    tmp_path: Path, recorded_tree: Path
) -> None:
    record = recorded_tree
    outcome = tmp_path / 'no-console-outcome.json'
    environment = os.environ.copy()
    current_pythonpath = environment.get('PYTHONPATH')
    environment['PYTHONPATH'] = (
        f'{_SOURCE_ROOT}{os.pathsep}{current_pythonpath}'
        if current_pythonpath
        else str(_SOURCE_ROOT)
    )
    wrapper = subprocess.Popen(
        (
            sys.executable,
            str(_FIXTURE),
            'windows-no-console-cleanup',
            str(record),
            str(outcome),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS | _WINDOWS_CREATE_NO_WINDOW,
        env=environment,
    )
    wrapper_result = finish_process(wrapper, timeout=5.0)
    result = cast(
        'dict[str, object]',
        json.loads(outcome.read_text(encoding='utf-8')),
    )
    recorded = cast('dict[str, int]', result['pids'])

    assert wrapper_result.returncode == 0, process_transcript(wrapper_result)
    assert result['console_process_count'] == 0
    assert result['ctrl_break_delivered'] is False
    assert all(not _pid_is_running(pid) for pid in recorded.values())


def test_native_pathext_resolution_and_batch_shim_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / 'Native Tool.EXE'
    shim = tmp_path / 'Hook Tool.CmD'
    executable.write_bytes(b'')
    shim.write_bytes(b'')

    def fail_case_insensitive_lookup(_candidate: Path) -> None:
        pytest.fail('native Windows lookup used simulated case folding')

    def fail_directory_enumeration(_path: Path) -> None:
        pytest.fail('native Windows lookup enumerated a directory')

    monkeypatch.setattr(
        executable_module,
        '_case_insensitive_file',
        fail_case_insensitive_lookup,
    )
    monkeypatch.setattr(Path, 'iterdir', fail_directory_enumeration)

    runnable = resolve_executable(
        'native tool',
        path_value=str(tmp_path),
        windows=True,
        pathext_value=None,
    )
    unsupported = resolve_executable(
        'hook tool',
        path_value=str(tmp_path),
        windows=True,
        pathext_value=None,
    )

    assert runnable.status is ExecutableStatus.RUNNABLE
    assert runnable.resolved == executable
    assert unsupported.status is ExecutableStatus.UNSUPPORTED_SHIM
    assert unsupported.resolved == shim


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_native_refused_assignment_obeys_override(allow_uncontained: bool) -> None:
    api = _RecordingNativeApi(refuse_assignment=True)
    backend = WindowsBackend(api)
    if not allow_uncontained:
        with pytest.raises(ContainmentError, match='assignment'):
            backend.start(
                Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
                allow_uncontained=False,
            )
        return

    managed = backend.start(
        Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
        allow_uncontained=True,
    )
    assert managed.containment is ContainmentState.UNCONTAINED
    managed.release()
    managed.process.wait(timeout=3.0)
    managed.finish()
    _close_managed_pipes(managed)


@pytest.mark.parametrize('allow_uncontained', [False, True])
def test_native_refused_job_creation_obeys_override(allow_uncontained: bool) -> None:
    api = _RecordingNativeApi(refuse_creation=True)
    backend = WindowsBackend(api)
    if not allow_uncontained:
        with pytest.raises(ContainmentError, match='creation'):
            backend.start(
                Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
                allow_uncontained=False,
            )
        return

    managed = backend.start(
        Step(1, (sys.executable, str(_FIXTURE), 'exit-without-stdin')),
        allow_uncontained=True,
    )
    assert managed.containment is ContainmentState.UNCONTAINED
    managed.release()
    managed.process.wait(timeout=3.0)
    managed.finish()
    _close_managed_pipes(managed)


@pytest.mark.parametrize(
    ('ctrl_break_result', 'ctrl_break_error'),
    [
        (False, None),
        (None, OSError('native Ctrl+Break refused by test')),
    ],
)
def test_native_uncontained_failed_ctrl_break_forces_direct_cleanup(
    ctrl_break_result: bool | None,
    ctrl_break_error: OSError | None,
) -> None:
    api = _RecordingNativeApi(
        ctrl_break_error=ctrl_break_error,
        ctrl_break_result=ctrl_break_result,
    )
    managed = _UncontainedManagedStep(
        Step(1, (sys.executable, str(_FIXTURE), 'sleep', '10')),
        platform_name='win32',
        windows_api=api,
    )
    pid = managed.process.pid
    managed.stdin.close()

    try:
        managed.forward_signal(_WINDOWS_SIGBREAK)
        managed.terminate_tree(0.0)
    finally:
        managed.finish()
        _close_managed_pipes(managed)

    assert api.ctrl_break_process_groups == [pid]
    assert not _pid_is_running(pid)


def test_native_uncontained_successful_ctrl_break_targets_direct_child(
    tmp_path: Path,
) -> None:
    signal_file = tmp_path / 'uncontained-signal.json'
    ready_file = tmp_path / 'uncontained-signal.ready'
    api = _RecordingNativeApi()
    managed = _UncontainedManagedStep(
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
        platform_name='win32',
        windows_api=api,
    )
    pid = managed.process.pid
    managed.stdin.close()
    deadline = time.monotonic() + 3.0
    while not ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.005)

    try:
        assert ready_file.exists()
        managed.forward_signal(signal.SIGINT)
        assert managed.process.wait(timeout=3.0) == 0
    finally:
        managed.finish()
        _close_managed_pipes(managed)

    recorded = cast('dict[str, int]', json.loads(signal_file.read_text(encoding='utf-8')))
    executing_pid = int(ready_file.read_text(encoding='utf-8'))
    assert recorded == {'pid': executing_pid, 'signum': _WINDOWS_SIGBREAK}
    assert api.ctrl_break_process_groups == [pid]
    assert api.ctrl_break_results == [True]


def test_native_wrapper_force_exit_triggers_kill_on_close(recorded_tree: Path) -> None:
    record = recorded_tree
    environment = os.environ.copy()
    current_pythonpath = environment.get('PYTHONPATH')
    environment['PYTHONPATH'] = (
        f'{_SOURCE_ROOT}{os.pathsep}{current_pythonpath}'
        if current_pythonpath
        else str(_SOURCE_ROOT)
    )
    wrapper = subprocess.Popen(
        (
            sys.executable,
            str(_FIXTURE),
            'windows-force-wrapper-exit',
            str(record),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    wrapper_result = finish_process(wrapper, timeout=5.0)
    recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))
    deadline = time.monotonic() + 3.0
    while any(_pid_is_running(pid) for pid in recorded.values()) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert wrapper_result.returncode == 0xC0000005, process_transcript(wrapper_result)
    assert all(not _pid_is_running(pid) for pid in recorded.values())


def test_native_wrapper_interrupt_cleans_child(tmp_path: Path) -> None:
    signal_file = tmp_path / 'signal.json'
    ready_file = tmp_path / 'signal.ready'
    executor = ProcessExecutor(Diagnostics(StringIO()))

    def send_signal() -> None:
        deadline = time.monotonic() + 3.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if ready_file.exists():
            signal.raise_signal(_WINDOWS_SIGBREAK)

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
                4.0,
                allow_uncontained=False,
            )
    sender.join(timeout=1.0)

    assert not sender.is_alive()
    assert raised.value.signum == _WINDOWS_SIGBREAK
    recorded = cast('dict[str, int]', json.loads(signal_file.read_text(encoding='utf-8')))
    assert recorded['signum'] == _WINDOWS_SIGBREAK
    assert recorded['pid'] == int(ready_file.read_text(encoding='utf-8'))
    assert not _pid_is_running(recorded['pid'])


def test_native_wrapper_interrupt_cleans_descendant_tree(recorded_tree: Path) -> None:
    record = recorded_tree
    executor = ProcessExecutor(Diagnostics(StringIO()))

    def send_signal() -> None:
        published = Path(f'{record}.published')
        deadline = time.monotonic() + 3.0
        while not published.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if published.exists():
            signal.raise_signal(_WINDOWS_SIGBREAK)

    sender = threading.Thread(target=send_signal)
    with executor.signal_forwarding():
        sender.start()
        with pytest.raises(ForwardedSignal):
            executor.run(
                Step(
                    1,
                    (
                        sys.executable,
                        str(_FIXTURE),
                        'child-tree',
                        '10',
                        str(record),
                    ),
                ),
                b'{}\n',
                4.0,
                allow_uncontained=False,
            )
    sender.join(timeout=1.0)
    recorded = cast('dict[str, int]', json.loads(record.read_text(encoding='utf-8')))

    assert not sender.is_alive()
    assert all(not _pid_is_running(pid) for pid in recorded.values())
