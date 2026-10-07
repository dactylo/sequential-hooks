import importlib.metadata
import io
import json
import os
import signal
import sys
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, cast

import pytest

from sequential_hooks import _cli
from sequential_hooks._arguments import InvocationError, parse_hook_argv
from sequential_hooks._executor import ForwardedSignal
from sequential_hooks.hosts import FinalResponse, JsonObject, NativeResponse, _json
from tests import _helpers
from tests._distributions import subprocess_environment
from tests._helpers import invoke_cli, run_setup
from tests.hosts_example.broken import (
    RuntimeConsumeFailureHost,
    RuntimeFailureHost,
    RuntimeFinalizeFailureHost,
    RuntimeShouldRunFailureHost,
    RuntimeStepInputFailureHost,
    UnconstructibleHost,
)
from tests.hosts_example.host import ExampleHost, ExampleResponse

if TYPE_CHECKING:
    from sequential_hooks._doctor import DoctorOutput

_EXAMPLE_INPUT = b'{"tool":"shell","arguments":{"command":"echo hi"}}\n'
_EXPECTED_DOCTOR_HELP = (
    'usage: sequential-hooks doctor --agent NAME\n'
    '\n'
    "Check an agent's hook configuration and platform containment support without\n"
    'changing anything.\n'
    '\n'
    'options:\n'
    '  -h, --help              show this help message and exit\n'
    '  -a, --agent NAME        coding agent, such as agy, claude, or codex\n'
)
_EXPECTED_HELP = (
    'usage: sequential-hooks --agent NAME [options] STEP [ARG ...] [-- STEP [ARG ...]] ...\n'
    '       sequential-hooks doctor --agent NAME\n'
    '\n'
    'Run up to three hook commands in order as one coding-agent hook.\n'
    '\n'
    'options:\n'
    '  -h, --help              show this help message and exit\n'
    '  --version               show the version and exit\n'
    '  -a, --agent NAME        coding agent, such as agy, claude, or codex\n'
    '  -e, --event EVENT       hook event, for agents whose payload omits it (agy)\n'
    '  --step-timeout SECONDS  time limit for each step (default: none)\n'
    '  --allow-on-failure      on pre-tool events, report a failed step and continue\n'
    '                          instead of blocking; on any event, run a step without\n'
    '                          containment when containment cannot be set up\n'
    '\n'
    'Separate steps with --; no step receives -- as an argument. Each step receives\n'
    "the agent's payload, as updated by earlier steps, and only its own arguments.\n"
    'Put wrapper options before the first step.\n'
    '\n'
    'example:\n'
    '  sequential-hooks --agent claude first-hook --check -- second-hook\n'
)


class _BrokenFlushFileDescriptorTextIO(io.StringIO):
    def fileno(self) -> int:
        return -1

    def flush(self) -> None:
        raise BrokenPipeError


class _BrokenFlushTextIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        raise BrokenPipeError


class _BrokenOutput(io.BytesIO):
    writes: int = 0

    def write(self, _data: object) -> int:
        self.writes += 1
        raise BrokenPipeError


class _BrokenPipeTextIO(io.StringIO):
    def write(self, _value: str) -> int:
        raise BrokenPipeError


class _BrokenWriteTextIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def write(self, _value: str) -> int:
        self.writes += 1
        raise BrokenPipeError


class _CliPathBoundary:
    def __init__(self, home: Path, cwd: Path, system_root: Path) -> None:
        self._cwd = cwd
        self._home = home
        self._system_root = system_root

    def __call__(self, _anchor: str) -> Path:
        return self._system_root

    def cwd(self) -> Path:
        return self._cwd

    def home(self) -> Path:
        return self._home


class _CountingInput(io.BytesIO):
    reads: int = 0

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


class _ErrorFlushTextIO(io.StringIO):
    def flush(self) -> None:
        raise OSError('text stream flush unavailable')


class _ErrorTextIO(io.StringIO):
    def write(self, _value: str) -> int:
        raise OSError('text stream unavailable')


class _ExecutorDouble:
    def __init__(self, _diagnostics: object) -> None:
        pass

    def run(self, *_args: object, **_kwargs: object) -> object:
        pytest.fail('child execution was not expected', pytrace=False)

    def signal_forwarding(self) -> object:
        return _NullContext()


class _FlushingTextIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _UnreadableInput(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise AssertionError(f'stdin was read with size {size}')


class _ResponsePropertyFailure:
    def __init__(self, failing_property: str) -> None:
        self._failing_property = failing_property

    @property
    def exit_code(self) -> int:
        if self._failing_property == 'exit_code':
            raise RuntimeError('exit code unavailable')
        return 0

    @property
    def stdout(self) -> bytes:
        if self._failing_property == 'stdout':
            raise RuntimeError('stdout unavailable')
        return b'{"verdict":"deny"}\n'


def _native_input(host: str, event: str) -> bytes:
    if host == 'agy':
        payload: dict[str, object] = {'stepIdx': 1}
        if event == 'PreToolUse':
            payload['toolCall'] = {'name': 'read_file', 'args': {}}
    else:
        payload = {
            'hook_event_name': event,
            'tool_name': 'Bash',
            'tool_input': {},
        }
        if event == 'PostToolUse':
            payload['tool_response'] = {}
    return f'{json.dumps(payload, separators=(",", ":"))}\n'.encode()


def _option_descriptions(help_text: str) -> dict[str, str]:
    lines = help_text.splitlines()
    descriptions: dict[str, str] = {}
    option = ''
    for line in lines[lines.index('options:') + 1 :]:
        if not line.startswith('  '):
            break
        if line.startswith('  -'):
            option, _separator, description = line.strip().partition('  ')
            descriptions[option] = description.strip()
        else:
            descriptions[option] = f'{descriptions[option]} {line.strip()}'
    return descriptions


def _raise_invariant(*_args: object, **_kwargs: object) -> NativeResponse:
    raise RuntimeError('broken invariant')


def _raise_signal(*_args: object, **_kwargs: object) -> NativeResponse:
    raise ForwardedSignal(signal.SIGTERM)


def _return_empty_response(*_args: object, **_kwargs: object) -> NativeResponse:
    return NativeResponse(b'{}\n')


def _safe_response(host: str, event: str) -> bytes:
    responses = {
        (
            'claude',
            'PreToolUse',
        ): (
            b'{"systemMessage":"[step 1: configuration]\\nspawn: sequential-hooks could not '
            b'safely run this hook","hookSpecificOutput":{"hookEventName":"PreToolUse",'
            b'"permissionDecision":"deny","permissionDecisionReason":"[step 1: configuration]'
            b'\\nspawn: sequential-hooks could not safely run this hook"}}\n'
        ),
        (
            'claude',
            'PostToolUse',
        ): (
            b'{"systemMessage":"[step 1: configuration]\\nspawn: sequential-hooks could not '
            b'safely run this hook","decision":"block","reason":"[step 1: configuration]'
            b'\\nspawn: sequential-hooks could not safely run this hook"}\n'
        ),
        (
            'codex',
            'PreToolUse',
        ): (
            b'{"systemMessage":"[step 1: configuration]\\nspawn: child result rejected; see '
            b'developer diagnostics","hookSpecificOutput":{"hookEventName":"PreToolUse",'
            b'"permissionDecision":"deny","permissionDecisionReason":"[step 1: configuration]'
            b'\\nspawn: child result rejected; see developer diagnostics"}}\n'
        ),
        (
            'codex',
            'PostToolUse',
        ): (
            b'{"systemMessage":"[step 1: configuration]\\nspawn: child result rejected; see '
            b'developer diagnostics","decision":"block","reason":"[step 1: configuration]'
            b'\\nspawn: child result rejected; see developer diagnostics"}\n'
        ),
        ('agy', 'PreToolUse'): (
            b'{"decision":"deny","reason":"[step 1: configuration]\\nspawn: '
            b'sequential-hooks could not safely run this hook"}\n'
        ),
        ('agy', 'PostToolUse'): b'{}\n',
    }
    return responses[(host, event)]


def _starts_in_lowercase_style(text: str) -> bool:
    first = text.split(maxsplit=1)[0]
    is_acronym = first.upper() == first and any(character.isalpha() for character in first)
    return text[0].islower() or is_acronym


def test_adapter_returning_none_uses_last_resort(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del example_hosts

    def return_none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(ExampleHost, 'adapter', return_none)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'example', '--event', 'BeforeTool', 'child'],
        io.BytesIO(_EXAMPLE_INPUT),
    )

    assert (status, stdout) == (2, b'')
    assert stderr == 'sequential-hooks: host example adapter failed: NoneType\n'


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_antigravity_configuration_error_event_authority_uses_argv(event: str) -> None:
    raw_input = (
        b'{"hook_event_name":"PostToolUse","stepIdx":1,'
        b'"toolCall":{"name":"read_file","args":{}}}\n'
    )

    status, stdout, _stderr = invoke_cli(
        ['--agent', 'agy', '--event', event, '--unknown'],
        io.BytesIO(raw_input),
    )

    assert status == 0
    assert stdout == _safe_response('agy', event)


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_antigravity_event_after_syntax_error_uses_last_resort(event: str) -> None:
    status, stdout, stderr = invoke_cli(
        ['--agent', 'agy', '--unknown', '--event', event],
        io.BytesIO(_native_input('agy', event)),
    )

    assert (status, stdout) == (2, b'')
    assert stderr == (
        'sequential-hooks: unknown wrapper option; use --help for supported syntax; '
        '--event is required for host agy\n'
    )


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (['--agent', 'agy', 'child'], '--event is required for host agy'),
        (['--agent', 'agy', '--event', 'Other', 'child'], 'does not serve event'),
    ],
)
def test_antigravity_without_a_trusted_event_uses_the_last_resort(
    argv: list[str],
    expected: str,
) -> None:
    status, stdout, stderr = invoke_cli(argv, io.BytesIO(b'{"stepIdx":1}\n'))

    assert (status, stdout) == (2, b'')
    assert expected in stderr


@pytest.mark.usefixtures('distribution_root')
def test_broken_pipe_writes_no_second_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, 'run_pipeline', _return_empty_response)
    stdout = _BrokenOutput()
    stderr = io.StringIO()

    monkeypatch.setattr(
        sys,
        'stdin',
        io.TextIOWrapper(io.BytesIO(_native_input('agy', 'PostToolUse')), encoding='utf-8'),
    )
    monkeypatch.setattr(sys, 'stdout', io.TextIOWrapper(stdout, encoding='utf-8'))
    monkeypatch.setattr(sys, 'stderr', stderr)

    status = _cli.main(['--agent', 'agy', '--event', 'PostToolUse', 'hook'])

    assert status == 0
    assert stdout.writes == 1
    assert stderr.getvalue() == ''


def test_cli_import_stays_off_the_cold_paths() -> None:
    code = (
        'import sys\n'
        'import sequential_hooks._cli\n'
        "assert 'sequential_hooks._doctor' not in sys.modules\n"
        "assert 'importlib.metadata' not in sys.modules\n"
        "assert 'ctypes' not in sys.modules\n"
        "if sys.platform != 'win32':\n"
        '    from sequential_hooks._containment import current_backend\n'
        '    current_backend()\n'
        "    assert 'ctypes' not in sys.modules\n"
    )
    run_setup(
        [sys.executable, '-c', code],
        env=subprocess_environment(),
    )


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_configuration_error_event_authority_uses_stream(host: str) -> None:
    status, stdout, _stderr = invoke_cli(
        ['--agent', host, '--event', 'PostToolUse', '--unknown'],
        io.BytesIO(_native_input(host, 'PreToolUse')),
    )

    assert status == 0
    assert stdout == _safe_response(host, 'PreToolUse')


@pytest.mark.usefixtures('distribution_root')
def test_configuration_error_payload_is_parsed_once_by_strict_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load_object_text = _json.load_object_text
    calls = 0

    def counting_load_object_text(text: str) -> JsonObject:
        nonlocal calls
        calls += 1
        return real_load_object_text(text)

    monkeypatch.setattr(_json, 'load_object_text', counting_load_object_text)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', 'hook'],
        io.BytesIO(b'{"hook_event_name":"PreToolUse"}\n'),
    )

    assert status == 0
    assert stdout == _safe_response('claude', 'PreToolUse')
    assert 'sequential-hooks: native hook input is invalid: ' in stderr
    assert calls == 1


@pytest.mark.parametrize('failing_property', ['stdout', 'exit_code'])
def test_configuration_response_property_failure_uses_last_resort(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_property: str,
) -> None:
    del example_hosts
    response = _ResponsePropertyFailure(failing_property)

    def return_response(*_args: object, **_kwargs: object) -> FinalResponse:
        return response

    monkeypatch.setattr(ExampleHost, 'configuration_response', return_response)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'example', '--event', 'BeforeTool', '--unknown'],
        io.BytesIO(_EXAMPLE_INPUT),
    )

    assert (status, stdout) == (2, b'')
    assert stderr == (
        'sequential-hooks: wrapper invocation is invalid; host example configuration '
        'response failed: RuntimeError\n'
    )
    assert len(stderr.splitlines()) == 1


@pytest.mark.usefixtures('distribution_root')
def test_context_dependent_failure_reads_stdin_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, 'ProcessExecutor', _ExecutorDouble)
    stdin = _CountingInput(_native_input('claude', 'PostToolUse'))

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', '--private-invalid-option'],
        stdin,
    )

    assert status == 0
    assert stdin.reads == 1
    assert stdout == _safe_response('claude', 'PostToolUse')
    assert stderr == (
        'sequential-hooks: configuration: spawn: sequential-hooks could not s'
        'afely run this hook\nsequential-hooks: wrapper invocation is invalid\n'
    )


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    'argv',
    [
        ['--help'],
        ['--version'],
        ['doctor', '--help'],
        ['doctor'],
        ['doctor', 'extra'],
        ['--help', 'extra'],
        ['--version', 'extra'],
        ['--agent', 'unknown', 'hook'],
        ['--agent', 'agy', '--event', 'Other', 'hook'],
    ],
)
def test_context_free_cli_paths_do_not_read_stdin(argv: list[str]) -> None:
    status, stdout, _stderr = invoke_cli(argv, _UnreadableInput())

    assert status in (0, 2)
    if status == 2:
        assert stdout == b''


def test_context_free_text_uses_the_documented_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, '_version', lambda: 'sequential-hooks 9.9.9\n')

    help_status, help_stdout, help_stderr = invoke_cli(['--help'], _UnreadableInput())
    version_status, version_stdout, version_stderr = invoke_cli(
        ['--version'],
        _UnreadableInput(),
    )
    doctor_status, doctor_stdout, doctor_stderr = invoke_cli(
        ['doctor', '--help'],
        _UnreadableInput(),
    )
    invalid_status, invalid_stdout, invalid_stderr = invoke_cli(
        ['doctor'],
        _UnreadableInput(),
    )

    assert (help_status, help_stdout, help_stderr) == (0, _EXPECTED_HELP.encode(), '')
    assert (version_status, version_stdout, version_stderr) == (
        0,
        b'sequential-hooks 9.9.9\n',
        '',
    )
    assert (doctor_status, doctor_stdout, doctor_stderr) == (
        0,
        _EXPECTED_DOCTOR_HELP.encode(),
        '',
    )
    assert (invalid_status, invalid_stdout) == (2, b'')
    assert invalid_stderr == ('sequential-hooks: invalid doctor invocation; use doctor --help\n')


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    ('host', 'detail'),
    [
        ('claude', 'sequential-hooks could not safely run this hook'),
        ('codex', "'sequential-hooks could not safely run this hook'"),
    ],
)
def test_deep_native_input_yields_invalid_input_response(
    host: str,
    detail: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, 'ProcessExecutor', _ExecutorDouble)
    depth = 600
    payload = (
        b'{"hook_event_name":"PreToolUse","tool_name":"probe","tool_input":'
        + b'{"k":' * depth
        + b'{}'
        + b'}' * depth
        + b'}'
    )

    status, stdout, stderr = invoke_cli(
        ['--agent', host, 'unused-child'],
        io.BytesIO(payload),
    )

    assert status == 0
    assert stdout == _safe_response(host, 'PreToolUse')
    assert stderr == (
        'sequential-hooks: configuration: spawn: '
        f'{detail}'
        '\nsequential-hooks: native hook input is invalid: native input nests '
        'too deeply\n'
    )


@pytest.mark.parametrize('host', ['codex', 'example'])
def test_doctor_cli_accepts_agent_option(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []

    def fake_run_doctor(selected_host: str, **kwargs: object) -> int:
        received.append(selected_host)
        write = getattr(kwargs['stream'], 'write', None)
        assert callable(write)
        write('ok\n')
        return 0

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fake_run_doctor)
    status, stdout, stderr = invoke_cli(['doctor', '--agent', host], _UnreadableInput())

    assert status == 0
    assert stdout == b'ok\n'
    assert stderr == ''
    assert received == [host]


def test_doctor_cli_accepts_short_agent_option(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []

    def fake_run_doctor(selected_host: str, **_kwargs: object) -> int:
        received.append(selected_host)
        return 0

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fake_run_doctor)
    status, stdout, stderr = invoke_cli(['doctor', '-a', 'codex'], _UnreadableInput())

    assert status == 0
    assert stdout == b''
    assert stderr == ''
    assert received == ['codex']


@pytest.mark.usefixtures('distribution_root')
def test_doctor_cli_discovers_project_configuration_from_given_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    source = project / '.claude/settings.json'
    home.mkdir()
    source.parent.mkdir(parents=True)
    source.write_text('{}\n', encoding='utf-8')

    def fake_doctor_system_root(
        selected_cwd: Path,
        *,
        windows: bool,
        environment: object,
    ) -> Path:
        assert selected_cwd == project
        assert windows is (os.name == 'nt')
        assert environment is os.environ
        return tmp_path

    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(_cli, '_doctor_system_root', fake_doctor_system_root)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty-bin'))
    previous_cwd = Path.cwd()

    status, stdout, stderr = invoke_cli(
        ['doctor', '--agent', 'claude'], _UnreadableInput(), cwd=project
    )

    assert status == 1
    displayed_source = source.as_posix().encode('unicode_escape').decode('ascii')
    assert f'- [project] {displayed_source}: exists\n'.encode() in stdout
    assert stderr == ''
    assert Path.cwd() == previous_cwd


@pytest.mark.parametrize('host', ['claude', 'codex', 'agy'])
def test_doctor_cli_injects_environment_at_text_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'work'
    system_root = tmp_path / 'machine-root'

    def fake_doctor_system_root(
        selected_cwd: Path,
        *,
        windows: bool,
        environment: object,
    ) -> Path:
        assert selected_cwd == cwd
        assert windows is (os.name == 'nt')
        assert environment is os.environ
        return system_root

    monkeypatch.setattr(_cli, '_doctor_system_root', fake_doctor_system_root)
    path_value = str(tmp_path / 'bin')
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(Path, 'cwd', lambda: cwd)
    monkeypatch.setenv('PATH', path_value)
    monkeypatch.setenv('PATHEXT', '.EXE;.COM')
    received: dict[str, object] = {}

    def fake_run_doctor(selected_host: str, **kwargs: object) -> int:
        received.update(host=selected_host, **kwargs)
        stream = cast('DoctorOutput', kwargs['stream'])
        stream.write(f'report for {selected_host}\n')
        return 0

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fake_run_doctor)
    status, stdout, stderr = invoke_cli(['doctor', '--agent', host], _UnreadableInput())

    assert status == 0
    assert stdout == f'report for {host}\n'.encode()
    assert stderr == ''
    assert received == {
        'host': host,
        'home': home,
        'cwd': cwd,
        'system_root': system_root,
        'path_value': path_value,
        'path_ext': '.EXE;.COM',
        'stream': received['stream'],
    }


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex', 'agy'])
def test_doctor_cli_runs_real_report_with_synthetic_boundaries(
    host: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    system_root = tmp_path / 'system'
    path_directory = tmp_path / 'bin'
    for directory in (home, cwd, system_root, path_directory):
        directory.mkdir()
    monkeypatch.setattr(_cli, 'Path', _CliPathBoundary(home, cwd, system_root))
    monkeypatch.setenv('PATH', str(path_directory))
    monkeypatch.delenv('PATHEXT', raising=False)
    status, stdout, stderr = invoke_cli(['doctor', '--agent', host], _UnreadableInput())
    report = stdout.decode('utf-8')

    assert status == 1
    assert f'agent: {host}\n' in report
    assert '\nlimitations:\n- ' in report
    assert home.as_posix().encode('unicode_escape').decode('ascii') in report
    if host != 'agy':
        assert system_root.as_posix().encode('unicode_escape').decode('ascii') in report
    assert stderr == ''


def test_doctor_help_flushes_successful_text_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    doctor_stdout = _FlushingTextIO()

    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', doctor_stdout)
    monkeypatch.setattr(sys, 'stderr', io.StringIO())

    status = _cli.main(['doctor', '--help'])

    assert status == 0
    assert doctor_stdout.getvalue() == _EXPECTED_DOCTOR_HELP
    assert doctor_stdout.flushes == 1


def test_doctor_help_propagates_descriptor_redirect_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _BrokenFlushFileDescriptorTextIO()
    original_stdout = _cli.sys.stdout
    monkeypatch.setattr(_cli.sys, 'stdout', stdout)

    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stderr', io.StringIO())

    try:
        with pytest.raises(OSError, match=r'.'):
            _cli.main(['doctor', '--help'])
    finally:
        monkeypatch.setattr(_cli.sys, 'stdout', original_stdout)


def test_doctor_help_propagates_non_broken_pipe_flush_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', _ErrorFlushTextIO())
    monkeypatch.setattr(sys, 'stderr', io.StringIO())

    with pytest.raises(OSError, match='text stream flush unavailable'):
        _cli.main(['doctor', '--help'])


def test_doctor_help_propagates_non_broken_pipe_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', _ErrorTextIO())
    monkeypatch.setattr(sys, 'stderr', io.StringIO())

    with pytest.raises(OSError, match='text stream unavailable'):
        _cli.main(['doctor', '--help'])


def test_doctor_help_suppresses_broken_pipe_from_text_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doctor_stdout = _BrokenFlushTextIO()
    stderr = io.StringIO()

    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', doctor_stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)

    status = _cli.main(['doctor', '--help'])

    assert status == 0
    assert doctor_stdout.getvalue() == _EXPECTED_DOCTOR_HELP
    assert doctor_stdout.flushes == 1
    assert stderr.getvalue() == ''


def test_doctor_help_suppresses_broken_pipe_from_text_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doctor_stdout = _BrokenWriteTextIO()
    stderr = io.StringIO()

    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', doctor_stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)

    status = _cli.main(['doctor', '--help'])

    assert status == 0
    assert doctor_stdout.writes == 1
    assert stderr.getvalue() == ''


def test_doctor_help_uses_text_stdout_without_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def fail_inspection(*_args: object, **_kwargs: object) -> int:
        raise AssertionError('doctor help inspected the host')

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fail_inspection)
    status, stdout, stderr = invoke_cli(['doctor', '--help'], _UnreadableInput())

    assert status == 0
    assert stdout == _EXPECTED_DOCTOR_HELP.encode()
    assert stderr == ''


@pytest.mark.parametrize('report_status', [1, 2])
def test_doctor_preserves_report_status_after_broken_text_write(
    report_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doctor_stdout = _BrokenWriteTextIO()
    stderr = io.StringIO()

    def write_report(_host: str, **kwargs: object) -> int:
        stream = cast('DoctorOutput', kwargs['stream'])
        stream.write('first report line\n')
        stream.write('remaining report line\n')
        return report_status

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', write_report)

    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', doctor_stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)

    status = _cli.main(['doctor', '--agent', 'claude'])

    assert status == report_status
    assert doctor_stdout.writes == 1
    assert stderr.getvalue() == ''


def test_doctor_system_root_keeps_posix_anchor() -> None:
    checkout = Path('/work/project')

    root = _cli._doctor_system_root(
        checkout,
        windows=False,
        environment={'SystemDrive': 'C:'},
    )

    assert root == Path('/')


@pytest.mark.parametrize(
    ('environment', 'expected'),
    [
        ({'SystemDrive': 'C:'}, PureWindowsPath('C:/')),
        ({'SystemRoot': r'C:\Windows'}, PureWindowsPath('C:/')),
        ({'ProgramFiles': r'C:\Program Files'}, PureWindowsPath('C:/')),
    ],
)
def test_doctor_system_root_uses_windows_machine_location(
    environment: dict[str, str],
    expected: PureWindowsPath,
) -> None:
    checkout = Path('/non-system-drive/project')

    root = _cli._doctor_system_root(
        checkout,
        windows=True,
        environment=environment,
    )

    assert PureWindowsPath(root) == expected


@pytest.mark.parametrize(
    ('help_text', 'options'),
    [
        (
            _cli._HELP,
            [
                '-h, --help',
                '--version',
                '-a, --agent NAME',
                '-e, --event EVENT',
                '--step-timeout SECONDS',
                '--allow-on-failure',
            ],
        ),
        (_cli._DOCTOR_HELP, ['-h, --help', '-a, --agent NAME']),
    ],
    ids=['hook', 'doctor'],
)
def test_every_help_option_has_a_lowercase_description(
    help_text: str,
    options: list[str],
) -> None:
    descriptions = _option_descriptions(help_text)
    problems = [
        f'{option}: {description!r}'
        for option, description in descriptions.items()
        if (
            not description
            or not _starts_in_lowercase_style(description)
            or description.endswith('.')
        )
    ]
    assert list(descriptions) == options
    assert problems == []


@pytest.mark.usefixtures('distribution_root')
def test_forwarded_signal_returns_shell_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, 'run_pipeline', _raise_signal)

    status, stdout, _stderr = invoke_cli(
        ['--agent', 'claude', 'hook'],
        io.BytesIO(_native_input('claude', 'PreToolUse')),
    )

    assert status == 128 + signal.SIGTERM
    assert stdout == b''


@pytest.mark.usefixtures('distribution_root')
def test_hook_invocation_does_not_read_version_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_version(_distribution: str) -> str:
        raise AssertionError('hook invocation read version metadata')

    monkeypatch.setattr(importlib.metadata, 'version', forbidden_version)
    monkeypatch.setattr(_cli, 'run_pipeline', _return_empty_response)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PostToolUse', 'hook'],
        io.BytesIO(_native_input('agy', 'PostToolUse')),
    )

    assert (status, stdout, stderr) == (0, b'{}\n', '')


@pytest.mark.parametrize(
    'argv',
    [
        ['doctor'],
        ['doctor', '--agent'],
        ['doctor', '--agent', 'claude', '--agent', 'codex'],
        ['doctor', '--agent', 'claude', '-a', 'codex'],
        ['doctor', '--agent', '--event'],
        ['doctor', '--event', 'PreToolUse'],
        ['doctor', '--step-timeout', '1'],
        ['doctor', '--allow-on-failure'],
        ['doctor', '--version'],
        ['doctor', '-h', 'extra'],
        ['doctor', '--help', 'extra'],
        ['doctor', 'extra'],
    ],
)
def test_invalid_doctor_syntax_returns_two_without_inspection_or_stdin(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def fail_inspection(*_args: object, **_kwargs: object) -> int:
        raise AssertionError('invalid doctor syntax inspected the host')

    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fail_inspection)
    status, stdout, stderr = invoke_cli(argv, _UnreadableInput())

    assert status == 2
    assert stdout == b''
    assert stderr == 'sequential-hooks: invalid doctor invocation; use doctor --help\n'


@pytest.mark.usefixtures('distribution_root')
def test_invalid_native_input_does_not_expose_payload_text() -> None:
    sentinel = 'PAYLOAD_PRIVATE_SECRET_' + ('z' * 20_000)
    raw_input = _native_input('claude', 'PostToolUse').replace(
        b'"tool_input":{}',
        f'"private":"{sentinel}"'.encode(),
    )

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', 'hook'],
        io.BytesIO(raw_input),
    )

    assert status == 0
    assert stdout == _safe_response('claude', 'PostToolUse')
    assert stderr == (
        'sequential-hooks: configuration: spawn: sequential-hooks could not s'
        'afely run this hook\nsequential-hooks: native hook input is invalid: '
        'Claude Code input requires tool_input\n'
    )
    assert sentinel.encode() not in stdout
    assert sentinel not in stderr


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    ('host', 'event'),
    [
        ('claude', 'PreToolUse'),
        ('claude', 'PostToolUse'),
        ('codex', 'PreToolUse'),
        ('codex', 'PostToolUse'),
        ('agy', 'PreToolUse'),
        ('agy', 'PostToolUse'),
    ],
)
def test_invalid_native_input_produces_one_safe_response(host: str, event: str) -> None:
    argv = ['--agent', host]
    if host == 'agy':
        argv.extend(['--event', event])
    argv.append('hook')
    raw_input = (
        f'{{"hook_event_name":"{event}"}}'.encode() if host != 'agy' else b'{"stepIdx":"bad"}'
    )

    status, stdout, _stderr = invoke_cli(argv, io.BytesIO(raw_input))

    assert status == 0
    assert stdout.endswith(b'\n')
    assert stdout.count(b'\n') == 1


@pytest.mark.usefixtures('distribution_root')
def test_invariant_secret_is_absent_from_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = 'INVARIANT_PRIVATE_SECRET_' + ('y' * 20_000)

    def raise_secret(*_args: object, **_kwargs: object) -> NativeResponse:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(_cli, 'run_pipeline', raise_secret)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'codex', 'hook'],
        io.BytesIO(_native_input('codex', 'PostToolUse')),
    )

    assert status == 2
    assert stdout == b''
    assert stderr == 'sequential-hooks: unexpected internal failure\n'
    assert sentinel.encode() not in stdout
    assert sentinel not in stderr


def test_invoke_cli_decodes_text_stdin_as_utf8_with_non_utf8_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = io.TextIOWrapper

    def ascii_default_wrapper(
        buffer: io.BytesIO,
        *,
        encoding: str | None = None,
        newline: str | None = None,
    ) -> io.TextIOWrapper:
        return wrapper(buffer, encoding=encoding or 'ascii', newline=newline)

    def echo_text_input(_argv: object) -> int:
        sys.stdout.write(sys.stdin.read())
        return 0

    monkeypatch.setattr(io, 'TextIOWrapper', ascii_default_wrapper)
    monkeypatch.setattr(_helpers, 'main', echo_text_input)

    status, stdout, stderr = invoke_cli([], b'caf\xc3\xa9')

    assert status == 0
    assert stdout == b'caf\xc3\xa9'
    assert stderr == ''


@pytest.mark.usefixtures('distribution_root')
def test_known_antigravity_context_translates_invalid_wrapper_syntax() -> None:
    status, stdout, _stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PostToolUse', '--unknown'],
        io.BytesIO(_native_input('agy', 'PostToolUse')),
    )

    assert status == 0
    assert stdout == b'{}\n'


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_known_host_without_stream_event_is_context_error(host: str) -> None:
    status, stdout, stderr = invoke_cli(
        ['--agent', host, '--unknown'],
        io.BytesIO(b'{}'),
    )

    assert status == 2
    assert stdout == b''
    assert 'event' in stderr


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('agent_before_error', [False, True])
def test_known_stream_event_translates_invalid_wrapper_syntax(
    host: str, agent_before_error: bool
) -> None:
    raw_input = _native_input(host, 'PreToolUse')
    argv = ['--agent', host, '--unknown'] if agent_before_error else ['--unknown', '--agent', host]

    status, stdout, stderr = invoke_cli(
        argv,
        io.BytesIO(raw_input),
    )

    if agent_before_error:
        assert (status, stdout) == (0, _safe_response(host, 'PreToolUse'))
        assert 'sequential-hooks: wrapper invocation is invalid\n' in stderr
    else:
        assert (status, stdout) == (2, b'')
        assert stderr == (
            'sequential-hooks: unknown wrapper option; use --help for supported syntax\n'
        )


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (['--agent', 'missing', 'child'], "unknown host 'missing'"),
        (['--agent', 'outdated', '--event', 'BeforeTool', 'child'], 'implements host API 0'),
        (['--agent', 'unimportable', '--event', 'BeforeTool', 'child'], 'failed to load'),
        (['--agent', 'unconstructible', '--event', 'BeforeTool', 'child'], 'failed to load'),
        (['--agent', 'example', 'child'], '--event is required for host example'),
        (['--agent', 'example', '--event', 'Other', 'child'], 'does not serve event'),
        (
            ['--agent', 'exploding', '--event', 'BeforeTool', 'child'],
            'adapter failed: RuntimeError',
        ),
        (
            ['--agent', 'unanswering', '--event', 'BeforeTool', '--bogus', 'child'],
            'configuration response failed: RuntimeError',
        ),
    ],
)
def test_last_resort_writes_nothing_and_exits_two(
    example_hosts: Path,
    argv: list[str],
    expected: str,
) -> None:
    del example_hosts
    status, stdout, stderr = invoke_cli(argv, io.BytesIO(_EXAMPLE_INPUT))

    assert status == 2
    assert stdout == b''
    lines = stderr.splitlines()
    assert len(lines) == 1
    assert expected in lines[-1]


def test_missing_configuration_response_uses_last_resort(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del example_hosts

    def return_no_response(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(ExampleHost, 'configuration_response', return_no_response)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'example', '--event', 'BeforeTool', '--unknown'],
        io.BytesIO(_EXAMPLE_INPUT),
    )

    assert (status, stdout) == (2, b'')
    assert stderr == (
        'sequential-hooks: wrapper invocation is invalid; host example returned no '
        'configuration response\n'
    )


def test_missing_version_metadata_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:

    def missing_version(_distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, 'version', missing_version)

    status, stdout, stderr = invoke_cli(['--version'], _UnreadableInput())

    assert (status, stdout) == (2, b'')
    assert 'installed distribution metadata for sequential-hooks is unavailable' in stderr


def test_multiline_host_factory_failure_is_one_visible_diagnostic(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del example_hosts

    def multiline_failure(_self: UnconstructibleHost) -> None:
        raise RuntimeError('cannot\nconstruct\tadapter\ragain')

    monkeypatch.setattr(UnconstructibleHost, '__init__', multiline_failure)
    status, stdout, stderr = invoke_cli(
        ['--agent', 'unconstructible', 'child'], io.BytesIO(_EXAMPLE_INPUT)
    )

    assert (status, stdout) == (2, b'')
    assert stderr == (
        "sequential-hooks: host 'unconstructible' failed to load: "
        'cannot\\nconstruct\\tadapter\\ragain\n'
    )


@pytest.mark.usefixtures('distribution_root')
def test_oversized_native_integer_returns_status_without_traceback() -> None:
    raw_input = (
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash",'
        b'"tool_input":{"value":' + (b'1' * 4_301) + b'}}\n'
    )

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', 'hook'],
        io.BytesIO(raw_input),
    )

    assert status == 2
    assert stdout == b''
    assert stderr == 'sequential-hooks: native event could not be established\n'
    assert 'Traceback' not in stderr


@pytest.mark.parametrize(
    ('argv', 'host', 'event'),
    [
        (['--unknown'], None, None),
        (['--agent', 'claude', '--unknown'], 'claude', None),
        (['--agent', 'agy', '--event', 'PreToolUse', '--unknown'], 'agy', 'PreToolUse'),
        (['--agent', 'agy', '--unknown', '--event', 'PreToolUse'], 'agy', None),
        (['--agent', 'agy', '--event', 'Other', '--unknown'], 'agy', 'Other'),
    ],
)
def test_parse_error_retains_only_safe_context(
    argv: list[str],
    host: str | None,
    event: str | None,
) -> None:
    with pytest.raises(InvocationError) as caught:
        parse_hook_argv(argv)

    assert caught.value.host == host
    assert caught.value.event == event


@pytest.mark.parametrize(
    ('argv', 'host', 'event'),
    [
        (['--agent', 'claude', 'hook'], 'claude', None),
        (['--agent', 'codex', 'hook'], 'codex', None),
        (['--agent', 'agy', '--event', 'PreToolUse', 'hook'], 'agy', 'PreToolUse'),
        (['--agent', 'agy', '--event', 'PostToolUse', 'hook'], 'agy', 'PostToolUse'),
    ],
)
def test_parse_hook_argv_accepts_agent_commands(
    argv: list[str],
    host: str,
    event: str | None,
) -> None:
    invocation = parse_hook_argv(argv)

    assert invocation.host == host
    assert invocation.event == event
    assert invocation.steps[0].argv == ('hook',)


def test_parse_hook_argv_accepts_agent_option() -> None:
    invocation = parse_hook_argv(
        [
            '--agent',
            'codex',
            '--step-timeout',
            '30',
            'first-hook',
            '--agent',
            'codex',
            '--',
            'second-hook',
            '--agent',
            'codex',
        ]
    )

    assert invocation.host == 'codex'
    assert invocation.step_timeout == 30
    assert invocation.steps[0].argv == ('first-hook', '--agent', 'codex')
    assert invocation.steps[1].argv == ('second-hook', '--agent', 'codex')


def test_parse_hook_argv_accepts_short_agent_option() -> None:
    invocation = parse_hook_argv(['-a', 'codex', 'hook'])

    assert invocation.host == 'codex'
    assert invocation.steps[0].argv == ('hook',)


def test_parse_hook_argv_accepts_short_event_option() -> None:
    invocation = parse_hook_argv(['-a', 'agy', '-e', 'PreToolUse', 'hook'])

    assert invocation.event == 'PreToolUse'


def test_parse_hook_argv_accepts_three_consecutive_steps() -> None:
    invocation = parse_hook_argv(['--agent', 'codex', 'one', '--', 'two', '--', 'three'])

    assert [(step.index, step.argv) for step in invocation.steps] == [
        (1, ('one',)),
        (2, ('two',)),
        (3, ('three',)),
    ]


@pytest.mark.parametrize(
    'argv',
    [
        [],
        ['hook'],
        ['--agent'],
        ['--agent', '--event', 'hook'],
        ['--agent', 'claude', '--agent', 'claude', 'hook'],
        ['--agent', 'claude', '-a', 'claude', 'hook'],
        ['--unknown', 'hook'],
        ['--agent', 'claude'],
        ['--agent', 'agy', '--event'],
        ['--agent', 'agy', '--event', '--allow-on-failure', 'hook'],
        ['--agent', 'agy', '--event', 'PreToolUse', '--event', 'PostToolUse', 'hook'],
        ['--agent', 'claude', '--step-timeout', 'hook'],
        ['--agent', 'claude', '--step-timeout', '0', 'hook'],
        ['--agent', 'claude', '--step-timeout', '-1', 'hook'],
        ['--agent', 'claude', '--step-timeout', 'inf', 'hook'],
        ['--agent', 'claude', '--step-timeout', 'nan', 'hook'],
        ['--agent', 'claude', '--step-timeout', '1', '--step-timeout', '2', 'hook'],
        ['--agent', 'claude', '--allow-on-failure', '--allow-on-failure', 'hook'],
        ['--agent', 'claude', '--', 'hook'],
        ['--agent', 'claude', 'hook', '--'],
        ['--agent', 'claude', 'hook', '--', '--', 'next'],
        ['--agent', 'claude', 'one', '--', 'two', '--', 'three', '--', 'four'],
    ],
)
def test_parse_hook_argv_rejects_invalid_grammar(argv: list[str]) -> None:
    with pytest.raises(InvocationError):
        parse_hook_argv(argv)


def test_parse_hook_argv_retains_literal_child_arguments() -> None:
    invocation = parse_hook_argv(
        [
            '--agent',
            'claude',
            '--step-timeout',
            '1.5',
            '--allow-on-failure',
            'hook',
            '--agent',
            'child',
            'a b',
            '$(touch nope)',
            '--',
            'second',
            '--event',
            'child-event',
        ]
    )

    assert invocation.step_timeout == 1.5
    assert invocation.allow_on_failure is True
    assert invocation.steps == (
        invocation.steps[0].__class__(1, ('hook', '--agent', 'child', 'a b', '$(touch nope)')),
        invocation.steps[0].__class__(2, ('second', '--event', 'child-event')),
    )


@pytest.mark.parametrize('options', [(), ('--bogus',), ('--event', 'BeforeTool')])
def test_payload_event_failure_uses_last_resort_on_each_resolution_path(
    example_hosts: Path,
    options: tuple[str, ...],
) -> None:
    del example_hosts
    status, stdout, stderr = invoke_cli(
        ['--agent', 'unresolvable-event', *options, 'child'], io.BytesIO(_EXAMPLE_INPUT)
    )

    assert status == 2
    assert stdout == b''
    assert len(stderr.splitlines()) == 1
    assert 'host unresolvable-event event resolution failed: RuntimeError' in stderr


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_payload_hosts_reject_an_explicit_event_through_tier_two(host: str) -> None:
    raw_input = b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{}}\n'

    status, stdout, stderr = invoke_cli(
        ['--agent', host, '--event', 'PreToolUse', 'child'], io.BytesIO(raw_input)
    )

    assert status == 0
    assert b'"permissionDecision":"deny"' in stdout
    assert f'sequential-hooks: --event is not accepted for host {host}\n' in stderr


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_payload_hosts_without_a_trusted_event_use_the_last_resort(host: str) -> None:
    status, stdout, stderr = invoke_cli(
        ['--agent', host, 'child'], io.BytesIO(b'{"hook_event_name":"Other"}\n')
    )

    assert (status, stdout) == (2, b'')
    assert stderr == 'sequential-hooks: native event could not be established\n'


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_post_event_translates_invalid_wrapper_syntax_exactly(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, 'ProcessExecutor', _ExecutorDouble)

    status, stdout, stderr = invoke_cli(
        ['--agent', host, '--invalid-wrapper-option'],
        io.BytesIO(_native_input(host, 'PostToolUse')),
    )

    expected_step = (
        'spawn: sequential-hooks could not safely run this hook'
        if host == 'claude'
        else "spawn: 'sequential-hooks could not safely run this hook'"
    )
    assert status == 0
    assert stdout == _safe_response(host, 'PostToolUse')
    assert stderr == (
        'sequential-hooks: configuration: '
        f'{expected_step}'
        '\nsequential-hooks: wrapper invocation is invalid\n'
    )


@pytest.mark.parametrize(
    'host_type',
    [
        RuntimeConsumeFailureHost,
        RuntimeFinalizeFailureHost,
        RuntimeShouldRunFailureHost,
        RuntimeStepInputFailureHost,
    ],
)
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_runtime_adapter_callback_failure_uses_last_resort(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_type: type[RuntimeFailureHost],
    *,
    allow_on_failure: bool,
) -> None:
    del example_hosts
    configuration_calls: list[str] = []

    def record_configuration_response(
        _self: RuntimeFailureHost,
        event: str,
        reason: str,
        diagnostics: object,
    ) -> ExampleResponse:
        del event, reason, diagnostics
        configuration_calls.append(host_type.name)
        return ExampleResponse(b'{"verdict":"deny"}\n')

    monkeypatch.setattr(
        RuntimeFailureHost, 'configuration_response', record_configuration_response
    )
    options = ['--allow-on-failure'] if allow_on_failure else []

    status, stdout, stderr = invoke_cli(
        [
            '--agent',
            host_type.name,
            '--event',
            'BeforeTool',
            *options,
            *(sys.executable, '-c', 'pass'),
        ],
        io.BytesIO(_EXAMPLE_INPUT),
    )

    assert status == 2
    assert stdout == b''
    assert stderr == 'sequential-hooks: unexpected internal failure\n'
    assert len(stderr.splitlines()) == 1
    assert configuration_calls == []


@pytest.mark.parametrize('failing_property', ['stdout', 'exit_code'])
def test_runtime_response_property_failure_uses_last_resort(
    example_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_property: str,
) -> None:
    del example_hosts
    response = _ResponsePropertyFailure(failing_property)

    def return_response(*_args: object, **_kwargs: object) -> FinalResponse:
        return response

    monkeypatch.setattr(_cli, 'run_pipeline', return_response)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'example', '--event', 'BeforeTool', *(sys.executable, '-c', 'pass')],
        io.BytesIO(_EXAMPLE_INPUT),
    )

    assert (status, stdout) == (2, b'')
    assert stderr == 'sequential-hooks: unexpected internal failure\n'
    assert len(stderr.splitlines()) == 1


def test_short_help_options_use_the_documented_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_inspection(*_args: object, **_kwargs: object) -> int:
        pytest.fail('doctor help inspected the agent')

    help_status, help_stdout, help_stderr = invoke_cli(['-h'], _UnreadableInput())
    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fail_inspection)
    doctor_status, doctor_stdout, doctor_stderr = invoke_cli(['doctor', '-h'], _UnreadableInput())

    assert (help_status, help_stdout, help_stderr) == (0, _EXPECTED_HELP.encode(), '')
    assert (doctor_status, doctor_stdout, doctor_stderr) == (0, _EXPECTED_DOCTOR_HELP.encode(), '')


@pytest.mark.usefixtures('distribution_root')
def test_translate_error_writes_response_despite_broken_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.BytesIO()

    monkeypatch.setattr(
        sys,
        'stdin',
        io.TextIOWrapper(io.BytesIO(_native_input('claude', 'PreToolUse')), encoding='utf-8'),
    )
    monkeypatch.setattr(sys, 'stdout', io.TextIOWrapper(stdout, encoding='utf-8'))
    monkeypatch.setattr(sys, 'stderr', _BrokenPipeTextIO())

    status = _cli.main(['--agent', 'claude', '--invalid-wrapper-option'])

    assert stdout.getvalue()
    assert status == 0


@pytest.mark.parametrize(
    'argv',
    [
        ['--help'],
        ['--version'],
        ['doctor', '--help'],
        ['doctor', '--agent', 'claude'],
        ['--agent', 'claude', 'first-hook'],
        ['--agent', 'claude', '--bogus'],
    ],
    ids=['help', 'version', 'doctor-help', 'doctor', 'hook', 'invalid-hook'],
)
def test_unavailable_stdout_runs_nothing_and_exits_two(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> int:
        raise AssertionError('work started without standard output')

    stderr = io.StringIO()
    monkeypatch.setattr('sequential_hooks._doctor.run_doctor', fail)
    monkeypatch.setattr(_cli, 'load_host', fail)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(_UnreadableInput(), encoding='utf-8'))
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', stderr)

    status = _cli.main(argv)

    assert status == 2
    assert stderr.getvalue() == 'sequential-hooks: standard output is unavailable\n'


@pytest.mark.usefixtures('distribution_root')
def test_unexpected_failure_with_context_uses_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, 'run_pipeline', _raise_invariant)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PostToolUse', 'hook'],
        io.BytesIO(_native_input('agy', 'PostToolUse')),
    )

    assert status == 2
    assert stdout == b''
    assert stderr == 'sequential-hooks: unexpected internal failure\n'


@pytest.mark.usefixtures('distribution_root')
def test_unknown_option_secret_is_absent_from_translated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = 'OPTION_PRIVATE_SECRET_' + ('x' * 20_000)
    monkeypatch.setattr(_cli, 'ProcessExecutor', _ExecutorDouble)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', f'--{sentinel}'],
        io.BytesIO(_native_input('claude', 'PreToolUse')),
    )

    assert status == 0
    assert stdout == _safe_response('claude', 'PreToolUse')
    assert stderr == (
        'sequential-hooks: configuration: spawn: sequential-hooks could not s'
        'afely run this hook\nsequential-hooks: wrapper invocation is invalid\n'
    )
    assert sentinel.encode() not in stdout
    assert sentinel not in stderr


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('failure_option', [[], ['--allow-on-failure']])
def test_valid_explicit_null_antigravity_post_tool_skips_child(
    failure_option: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, 'ProcessExecutor', _ExecutorDouble)

    status, stdout, stderr = invoke_cli(
        [
            '--agent',
            'agy',
            '--event',
            'PostToolUse',
            *failure_option,
            'hook',
        ],
        io.BytesIO(b'{"stepIdx":1,"toolCall":null}\n'),
    )

    assert status == 0
    assert stdout == b'{}\n'
    assert stderr == ''


@pytest.mark.usefixtures('distribution_root')
def test_valid_hook_reads_stdin_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, 'run_pipeline', _return_empty_response)
    stdin = _CountingInput(_native_input('agy', 'PostToolUse'))

    status, stdout, stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PostToolUse', 'hook'],
        stdin,
    )

    assert status == 0
    assert stdin.reads == 1
    assert stdout == b'{}\n'
    assert stderr == ''


@pytest.mark.usefixtures('distribution_root')
def test_valid_native_payload_is_parsed_once_by_strict_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load_object_text = _json.load_object_text
    calls = 0

    def counting_load_object_text(text: str) -> JsonObject:
        nonlocal calls
        calls += 1
        return real_load_object_text(text)

    monkeypatch.setattr(_json, 'load_object_text', counting_load_object_text)
    monkeypatch.setattr(_cli, 'run_pipeline', _return_empty_response)

    status, stdout, stderr = invoke_cli(
        ['--agent', 'claude', 'hook'],
        io.BytesIO(_native_input('claude', 'PreToolUse')),
    )

    assert status == 0
    assert stdout == b'{}\n'
    assert stderr == ''
    assert calls == 1
