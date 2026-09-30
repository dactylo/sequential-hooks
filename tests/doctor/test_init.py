import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TextIO, cast

import pytest

from sequential_hooks import _doctor
from sequential_hooks._containment import _windows_api
from sequential_hooks._doctor import (
    _common,
    _inspect_host,
    _render_report,
    run_doctor,
)
from sequential_hooks._doctor._command import inspect_command
from sequential_hooks._doctor._types import (
    ConfigSource,
    DoctorReport,
    Finding,
    FindingSeverity,
    InspectedCommand,
    LaunchSemantics,
    PlatformReport,
    Registration,
    SourceKind,
)
from sequential_hooks._executable import ExecutableResolution, ExecutableStatus

if TYPE_CHECKING:
    from collections.abc import Callable

_DISPATCH_CASES: tuple[tuple[str, str, object, str], ...] = (
    (
        'claude',
        '.claude/settings.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^Bash$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': ['--agent', 'claude', 'child'],
                                'timeout': 30,
                                'enabled': True,
                            }
                        ],
                    }
                ]
            }
        },
        'PreToolUse',
    ),
    (
        'codex',
        '.codex/hooks.json',
        {
            'hooks': {
                'PostToolUse': [
                    {
                        'matcher': '^Bash$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': ['--agent', 'codex', 'child'],
                                'timeout': 30,
                            }
                        ],
                    }
                ]
            }
        },
        'PostToolUse',
    ),
    (
        'agy',
        '.gemini/config/hooks.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^run_command$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': (
                                    'sequential-hooks --agent agy --event PreToolUse child'
                                ),
                                'timeout': 30,
                            }
                        ],
                    }
                ]
            }
        },
        'PreToolUse',
    ),
)


class _ProbeApi:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def assign_pid(self, _job_handle: int, _pid: int) -> None:
        raise AssertionError('doctor assigned a process to the Job Object')

    def close_handle(self, _handle: int) -> None:
        self.operations.append('close')

    def create_kill_on_close_job(self) -> int:
        self.operations.append('create')
        return 17


def _create_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('')
    path.chmod(0o700)


def _escaped_path(path: Path) -> str:
    return str(path).encode('unicode_escape').decode('ascii')


def _guard_doctor_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = cast('Callable[..., TextIO | BinaryIO]', Path.open)

    def fail_operation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('doctor performed a forbidden operation')

    def guarded_open(
        path: Path,
        mode: str = 'r',
        *args: object,
        **kwargs: object,
    ) -> TextIO | BinaryIO:
        if any(flag in mode for flag in 'wax+'):
            raise AssertionError(f'doctor opened {path} with mutating mode {mode}')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded_open)
    for operation in (
        'chmod',
        'hardlink_to',
        'mkdir',
        'rename',
        'replace',
        'rmdir',
        'symlink_to',
        'touch',
        'unlink',
        'write_bytes',
        'write_text',
    ):
        monkeypatch.setattr(Path, operation, fail_operation)
    monkeypatch.setattr(os, 'popen', fail_operation)
    monkeypatch.setattr(os, 'system', fail_operation)
    monkeypatch.setattr(subprocess, 'Popen', fail_operation)
    monkeypatch.setattr(subprocess, 'call', fail_operation)
    monkeypatch.setattr(subprocess, 'check_call', fail_operation)
    monkeypatch.setattr(subprocess, 'check_output', fail_operation)
    monkeypatch.setattr(subprocess, 'getoutput', fail_operation)
    monkeypatch.setattr(subprocess, 'getstatusoutput', fail_operation)
    monkeypatch.setattr(subprocess, 'run', fail_operation)
    monkeypatch.setattr(_windows_api.NativeWindowsApi, 'assign_pid', fail_operation)


def _normalize_report_paths(output: str, report: DoctorReport, root: Path) -> str:
    paths = (
        *(source.path for source in report.sources),
        *((report.executable,) if report.executable is not None else ()),
    )
    replacements = {
        _escaped_path(path): f'$ROOT/{path.relative_to(root).as_posix()}'
        for path in paths
        if path.is_relative_to(root)
    }
    path_lines: dict[str, str] = {}
    if report.executable is not None:
        raw = _escaped_path(report.executable)
        if raw in replacements:
            path_lines[f'wrapper: {raw}\n'] = f'wrapper: {replacements[raw]}\n'
    for source in report.sources:
        raw = _escaped_path(source.path)
        if raw not in replacements:
            continue
        replacement = replacements[raw]
        state = 'exists' if source.exists else 'missing'
        path_lines[f'- [{source.kind.value}] {raw}: {state}\n'] = (
            f'- [{source.kind.value}] {replacement}: {state}\n'
        )
        path_lines[f'- source: {raw}\n'] = f'- source: {replacement}\n'
    host_label = {'agy': 'Antigravity', 'claude': 'Claude', 'codex': 'Codex'}.get(report.host)
    if host_label is not None:
        for finding in report.findings:
            for source in report.sources:
                raw = _escaped_path(source.path)
                if raw not in replacements:
                    continue
                intro = f'{host_label} configuration at {source.path} '
                if finding.message.startswith(intro):
                    prefix = f'- [{finding.severity.value}] {finding.code}: '
                    rendered_message = finding.message.encode('unicode_escape').decode('ascii')
                    raw_line = f'{prefix}{rendered_message}\n'
                    normalized_intro = f'{host_label} configuration at {replacements[raw]} '
                    path_lines[raw_line] = raw_line.replace(
                        f'{host_label} configuration at {raw} ', normalized_intro, 1
                    )
    return ''.join(path_lines.get(line, line) for line in output.splitlines(keepends=True))


def _registration(source: ConfigSource, command: str) -> Registration:
    return Registration(
        source=source,
        event='PreToolUse',
        matcher='^Bash$',
        command=command,
        args=('--agent', 'claude', 'child'),
        launch_semantics=LaunchSemantics.DIRECT_ARGV,
        command_windows=None,
        windows_launch_semantics=LaunchSemantics.DIRECT_ARGV,
        timeout=30.0,
        enabled=True,
        type='command',
    )


def _render_real_host(
    host: str,
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project/nested'
    system_root = tmp_path / 'system'
    executable_directory = tmp_path / 'bin'

    def fixed_version(_name: str) -> str:
        return '9.9.9'

    monkeypatch.setattr(_doctor.sys, 'platform', 'linux')
    monkeypatch.setattr(
        _doctor,
        'inspect_platform',
        lambda: PlatformReport('CPython', 'posix', None, ()),
    )
    monkeypatch.setattr(_doctor.importlib.metadata, 'version', fixed_version)
    report = _inspect_host(
        host,
        home=home,
        cwd=cwd,
        system_root=system_root,
        path_value=str(executable_directory),
        path_ext=None,
    )
    stream = io.StringIO()
    _render_report(report, stream)
    return _normalize_report_paths(stream.getvalue(), report, tmp_path)


def _tree_snapshot(
    roots: tuple[Path, ...],
) -> tuple[tuple[str, str, str, int, int, int, bytes | None], ...]:
    entries: list[tuple[str, str, str, int, int, int, bytes | None]] = []
    for root in roots:
        paths = (root, *sorted(root.rglob('*'), key=lambda path: path.as_posix()))
        for path in paths:
            metadata = path.stat()
            relative = path.relative_to(root).as_posix() or '.'
            kind = 'directory' if path.is_dir() else 'file'
            content = path.read_bytes() if path.is_file() else None
            entries.append(
                (
                    root.name,
                    relative,
                    kind,
                    metadata.st_mode,
                    metadata.st_mtime_ns,
                    metadata.st_size,
                    content,
                )
            )
    return tuple(entries)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize(
    ('commands', 'executables', 'expected_code'),
    [
        (('sequential-hooks child',), ('child',), 'host_missing'),
        (('sequential-hooks --agent claude child',), ('child',), 'wrong_host'),
        (
            ('sequential-hooks --agent codex --event PreToolUse child',),
            ('child',),
            'event_forbidden',
        ),
        (
            ('sequential-hooks --agent codex missing-child',),
            (),
            'child_executable_missing',
        ),
        (
            ('sequential-hooks --agent codex child',),
            ('child',),
            'outer_timeout_only',
        ),
        (
            (
                'sequential-hooks --agent codex child',
                'sequential-hooks --agent codex child',
            ),
            ('child',),
            'duplicate_wrapper',
        ),
        (
            ('sequential-hooks --agent codex child', 'child'),
            ('child',),
            'duplicate_child_registration',
        ),
        (
            ('sequential-hooks --agent codex "$(pwd)"',),
            (),
            'command_dynamic',
        ),
    ],
)
def test_codex_static_commands_reach_documented_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands: tuple[str, ...],
    executables: tuple[str, ...],
    expected_code: str,
) -> None:
    monkeypatch.setattr(_doctor.sys, 'platform', 'linux')
    home = tmp_path / 'home'
    cwd = tmp_path / 'work'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir()
    _create_executable(executable_directory / 'sequential-hooks')
    for executable in executables:
        _create_executable(executable_directory / executable)
    _write_json(
        home / '.codex/hooks.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^Bash$',
                        'hooks': [
                            {'type': 'command', 'command': command, 'timeout': 30}
                            for command in commands
                        ],
                    }
                ]
            }
        },
    )

    report = _inspect_host(
        'codex',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
    )

    assert expected_code in {finding.code for finding in report.findings}


def test_doctor_is_read_only_and_process_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    cwd.mkdir()
    source = home / '.claude/settings.json'
    _write_json(
        source,
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'private-child --password private-value',
                                'timeout': 30,
                            }
                        ]
                    }
                ]
            }
        },
    )
    before_content = source.read_bytes()
    before_metadata = source.stat()

    def fail_operation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('doctor performed a forbidden operation')

    monkeypatch.setattr(Path, 'write_bytes', fail_operation)
    monkeypatch.setattr(Path, 'write_text', fail_operation)
    monkeypatch.setattr(os, 'system', fail_operation)
    monkeypatch.setattr(subprocess, 'Popen', fail_operation)
    monkeypatch.setattr(subprocess, 'call', fail_operation)
    monkeypatch.setattr(subprocess, 'check_call', fail_operation)
    monkeypatch.setattr(subprocess, 'check_output', fail_operation)
    monkeypatch.setattr(subprocess, 'run', fail_operation)

    report = _inspect_host(
        'claude',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=None,
        path_ext=None,
    )
    stream = io.StringIO()
    _render_report(report, stream)

    after_metadata = source.stat()
    assert source.read_bytes() == before_content
    assert (after_metadata.st_mode, after_metadata.st_mtime_ns, after_metadata.st_size) == (
        before_metadata.st_mode,
        before_metadata.st_mtime_ns,
        before_metadata.st_size,
    )
    assert 'private-value' not in stream.getvalue()


def test_doctor_reports_an_unloadable_host_as_a_finding(tmp_path: Path) -> None:
    stream = io.StringIO()

    status = run_doctor(
        'missing',
        home=tmp_path,
        cwd=tmp_path,
        system_root=tmp_path,
        path_value=str(tmp_path),
        path_ext=None,
        stream=stream,
    )

    report = stream.getvalue()
    assert status == 1
    assert 'agent: missing' in report
    assert "[error] host_unavailable: unknown host 'missing'" in report
    assert 'sequential_hooks.hosts entry-point group' in report


def test_documented_claude_registration_is_not_actionable(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'work'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir()
    suffix = '.EXE' if sys.platform == 'win32' else ''
    for name in ('child', 'sequential-hooks'):
        _create_executable(executable_directory / f'{name}{suffix}')
    _write_json(
        home / '.claude/settings.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': [
                                    '--agent',
                                    'claude',
                                    '--step-timeout',
                                    '5',
                                    'child',
                                ],
                                'timeout': 30,
                            }
                        ]
                    }
                ]
            }
        },
    )
    stream = io.StringIO()

    status = run_doctor(
        'claude',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
        stream=stream,
    )

    assert status == 0
    assert '  enabled: true\n' in stream.getvalue()
    assert 'enabled_unknown' not in stream.getvalue()


@pytest.mark.parametrize('explicit_timeout', [True, False])
def test_inspect_agy_groups_findings_by_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_timeout: bool,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir()
    _create_executable(executable_directory / 'sequential-hooks')
    _create_executable(executable_directory / 'child')
    malformed = home / '.gemini/config/hooks.json'
    malformed.parent.mkdir(parents=True)
    malformed.write_text('{', encoding='utf-8')
    second: dict[str, object] = {
        'command': 'sequential-hooks --agent agy --event PostToolUse child',
    }
    if explicit_timeout:
        second['timeout'] = 30
    _write_json(
        cwd / '.agents/hooks.json',
        {
            'sequential-hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^run_command$',
                        'hooks': [
                            {
                                'command': 'sequential-hooks --agent agy --event PreToolUse '
                                '--step-timeout 30 missing-child',
                                'timeout': 30,
                            }
                        ],
                    },
                    {'matcher': '^read_file$', 'hooks': [second]},
                ],
            },
        },
    )
    monkeypatch.setattr(_doctor.sys, 'platform', 'linux')

    report = _inspect_host(
        'agy',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
    )

    expected = [
        'agy_json_malformed',
        'child_executable_missing',
        'agy_chain_budget_uncontained',
        *([] if explicit_timeout else ['agy_timeout_default_documented']),
        'wrong_event',
        'outer_timeout_only',
        'possible_matcher_overlap',
    ]
    assert [finding.code for finding in report.findings] == expected
    assert len(report.registrations) == 2
    assert report.findings[1] == Finding(
        FindingSeverity.ERROR,
        'child_executable_missing',
        'child executable cannot be resolved',
        'Install or configure a runnable child executable.',
    )
    assert report.findings[2] == Finding(
        FindingSeverity.WARNING,
        'agy_chain_budget_uncontained',
        'configured sequential-hooks chain budget leaves no room within the '
        'outer Antigravity handler timeout',
        'Set the outer Antigravity handler timeout above the configured chain budget.',
    )
    assert next(
        finding for finding in report.findings if finding.code == 'wrong_event'
    ) == Finding(
        FindingSeverity.ERROR,
        'wrong_event',
        'wrapper event does not match the registered event',
        'Use --event PreToolUse.',
    )
    stream = io.StringIO()
    _render_report(report, stream)
    rendered_codes = [
        line.split('] ', 1)[1].split(':', 1)[0]
        for line in stream.getvalue().splitlines()
        if line.startswith(('- [error]', '- [warning]', '- [info]'))
    ]
    assert rendered_codes == expected


def test_inspect_agy_keeps_windows_shell_contract_uninspectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    cwd.mkdir()
    _write_json(
        home / '.gemini/config/hooks.json',
        {
            'sequential-hooks': {
                'PreToolUse': [
                    {
                        'matcher': 'run_command',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': (
                                    'sequential-hooks --agent agy --event PostToolUse '
                                    '--step-timeout 20 first -- second'
                                ),
                                'timeout': 30,
                            }
                        ],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(_doctor.sys, 'platform', 'win32')

    report = _inspect_host(
        'agy',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=None,
        path_ext=None,
    )

    codes = [finding.code for finding in report.findings]
    assert codes.count('command_uninspectable') == 1
    assert 'agy_chain_budget_uncontained' not in codes
    assert 'wrong_event' not in codes


def test_inspect_agy_prioritizes_wrong_host_over_embedded_event_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir()
    _create_executable(executable_directory / 'sequential-hooks')
    _create_executable(executable_directory / 'child')
    _write_json(
        home / '.gemini/config/hooks.json',
        {
            'sequential-hooks': {
                'PreToolUse': [
                    {
                        'matcher': 'run_command',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': (
                                    'sequential-hooks --agent codex --event PreToolUse child'
                                ),
                                'timeout': 30,
                            }
                        ],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(_doctor.sys, 'platform', 'linux')

    report = _inspect_host(
        'agy',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
    )

    matching = [finding for finding in report.findings if finding.code == 'wrong_host']
    assert len(matching) == 1
    assert matching[0].correction == 'Use --agent agy.'
    assert 'event_forbidden' not in [finding.code for finding in report.findings]


def test_inspect_and_render_agy_exactly_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    cwd = project / 'nested'
    system_root = tmp_path / 'system'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir(parents=True)
    (project / '.git').mkdir()
    system_root.mkdir()
    _create_executable(executable_directory / 'sequential-hooks')
    _create_executable(executable_directory / 'child')
    _write_json(
        home / '.gemini/config/hooks.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^run_command$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': (
                                    'sequential-hooks --agent agy --event PostToolUse child'
                                ),
                                'timeout': 30,
                            }
                        ],
                    }
                ]
            }
        },
    )
    _write_json(cwd / '.agents/hooks.json', {})
    roots = (home, project, system_root, executable_directory)
    before = _tree_snapshot(roots)
    _guard_doctor_side_effects(monkeypatch)

    output = _render_real_host(
        'agy',
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert output == (
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        'agent: agy\n'
        'wrapper: $ROOT/bin/sequential-hooks\n'
        'version: 9.9.9\n'
        'interpreter: CPython\n'
        'containment: posix\n'
        'job object configurable: not applicable\n'
        '\n'
        'configuration sources:\n'
        '- [user] $ROOT/home/.gemini/config/hooks.json: exists\n'
        '- [project] $ROOT/project/.agents/hooks.json: missing\n'
        '- [project] $ROOT/project/nested/.agents/hooks.json: exists\n'
        '\n'
        'registrations:\n'
        '- source: $ROOT/home/.gemini/config/hooks.json\n'
        '  event: PreToolUse\n'
        '  matcher: ^run_command$\n'
        '  command: sequential-hooks --agent agy --event PostToolUse child\n'
        '  timeout: 30.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: posix_shell\n'
        '  windows launch: windows_shell\n'
        '\n'
        'findings:\n'
        '- [error] wrong_event: wrapper event does not match the registered event\n'
        '  correction: Use --event PreToolUse.\n'
        '- [info] outer_timeout_only: no internal step timeout is configured; only the outer '
        'handler timeout applies\n'
        '  correction: Add --step-timeout if each child needs an independent bound.\n'
        '\n'
        'limitations:\n'
        '- organization-managed Antigravity hook sources cannot be inspected until their '
        'exact readable source paths are established\n'
        '- product-internal or plugin-provided Antigravity hooks cannot be inspected until '
        'their exact readable source paths are established; plugin data is not scanned\n'
    )
    assert _tree_snapshot(roots) == before


def test_inspect_and_render_claude_exactly_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    cwd = project / 'nested'
    system_root = tmp_path / 'system'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir(parents=True)
    (project / '.git').mkdir()
    system_root.mkdir()
    for name in ('child', 'helper', 'other', 'sequential-hooks'):
        _create_executable(executable_directory / name)
    _write_json(
        home / '.claude/settings.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'matcher': '^Bash$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': [
                                    '--agent',
                                    'claude',
                                    '--step-timeout',
                                    '5',
                                    'child',
                                    '--',
                                    'other',
                                ],
                                'timeout': 20,
                                'enabled': True,
                            },
                            {
                                'type': 'command',
                                'command': 'child',
                                'args': list[str](),
                                'timeout': 4,
                                'enabled': False,
                            },
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': ['--agent', 'claude', 'child'],
                                'timeout': 30,
                                'enabled': True,
                            },
                        ],
                    }
                ],
                'PostToolUse': [
                    {
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'helper --api-token private-value',
                                'timeout': 30,
                            }
                        ]
                    }
                ],
            }
        },
    )
    _write_json(project / '.claude/settings.json', {})
    _write_json(cwd / '.claude/settings.local.json', {})
    managed = system_root / 'etc/claude-code'
    _write_json(managed / 'managed-settings.json', {})
    _write_json(managed / 'managed-settings.d/a.json', {})
    roots = (home, project, system_root, executable_directory)
    before = _tree_snapshot(roots)
    _guard_doctor_side_effects(monkeypatch)

    output = _render_real_host(
        'claude',
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert output == (
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        'agent: claude\n'
        'wrapper: $ROOT/bin/sequential-hooks\n'
        'version: 9.9.9\n'
        'interpreter: CPython\n'
        'containment: posix\n'
        'job object configurable: not applicable\n'
        '\n'
        'configuration sources:\n'
        '- [user] $ROOT/home/.claude/settings.json: exists\n'
        '- [project] $ROOT/project/.claude/settings.json: exists\n'
        '- [local] $ROOT/project/.claude/settings.local.json: missing\n'
        '- [project] $ROOT/project/nested/.claude/settings.json: missing\n'
        '- [local] $ROOT/project/nested/.claude/settings.local.json: exists\n'
        '- [managed] $ROOT/system/etc/claude-code/managed-settings.json: exists\n'
        '- [managed] $ROOT/system/etc/claude-code/managed-settings.d/a.json: exists\n'
        '\n'
        'registrations:\n'
        '- source: $ROOT/home/.claude/settings.json\n'
        '  event: PreToolUse\n'
        '  matcher: ^Bash$\n'
        '  command: sequential-hooks\n'
        '  timeout: 20.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: direct_argv\n'
        '  windows launch: direct_argv\n'
        '- source: $ROOT/home/.claude/settings.json\n'
        '  event: PreToolUse\n'
        '  matcher: ^Bash$\n'
        '  command: child\n'
        '  timeout: 4.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: direct_argv\n'
        '  windows launch: direct_argv\n'
        '- source: $ROOT/home/.claude/settings.json\n'
        '  event: PreToolUse\n'
        '  matcher: ^Bash$\n'
        '  command: sequential-hooks\n'
        '  timeout: 30.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: direct_argv\n'
        '  windows launch: direct_argv\n'
        '- source: $ROOT/home/.claude/settings.json\n'
        '  event: PostToolUse\n'
        '  matcher: any\n'
        '  command: helper --api-token [REDACTED]\n'
        '  timeout: 30.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: posix_shell\n'
        '  windows launch: host_tokenizer\n'
        '\n'
        'findings:\n'
        '- [warning] outer_timeout_too_small: outer handler timeout may expire before '
        'cleanup completes\n'
        '  correction: Set an outer handler timeout of at least 5 seconds.\n'
        '- [info] outer_timeout_only: no internal step timeout is configured; only the '
        'outer handler timeout applies\n'
        '  correction: Add --step-timeout if each child needs an independent bound.\n'
        '- [info] claude_exec_form_recommended: Claude Code command strings use a POSIX shell\n'
        '  correction: Use the exec form with explicit args.\n'
        '- [warning] duplicate_child_registration: literal child duplicates a wrapper step\n'
        '  correction: Remove the separately registered literal child.\n'
        '- [warning] duplicate_wrapper: event has repeated wrapper registrations\n'
        '  correction: Keep one wrapper registration for each event and matcher.\n'
        '- [warning] definite_matcher_overlap: definite same-event matcher overlap\n'
        '  correction: Make same-event matcher scopes disjoint.\n'
        '- [warning] duplicate_child_registration: literal child duplicates a wrapper step\n'
        '  correction: Remove the separately registered literal child.\n'
        '\n'
        'limitations:\n'
        '- server-managed Claude Code settings cannot be inspected as a local file\n'
        '- macOS com.anthropic.claudecode managed preferences or plist delivery cannot be '
        'inspected\n'
        '- Windows HKLM and HKCU SOFTWARE\\\\Policies\\\\ClaudeCode Settings registry delivery '
        'cannot be inspected\n'
        '- plugin-provided Claude Code hooks cannot be inspected without scanning plugin '
        'caches\n'
    )
    assert _tree_snapshot(roots) == before


def test_inspect_and_render_codex_exactly_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    cwd = project / 'nested'
    system_root = tmp_path / 'system'
    executable_directory = tmp_path / 'bin'
    cwd.mkdir(parents=True)
    (project / '.git').mkdir()
    system_root.mkdir()
    executable_directory.mkdir()
    _write_json(
        home / '.codex/hooks.json',
        {
            'hooks': {
                'PostToolUse': [
                    {
                        'matcher': '^Bash$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks --api-token private-value',
                            }
                        ],
                    }
                ]
            }
        },
    )
    (home / '.codex/config.toml').write_text('[features]\ncodex_hooks = true\n')
    (system_root / 'etc/codex').mkdir(parents=True)
    (system_root / 'etc/codex/config.toml').write_text('[features]\ncodex_hooks = true\n')
    _write_json(project / '.codex/hooks.json', {})
    (cwd / '.codex').mkdir()
    (cwd / '.codex/config.toml').write_text('[features]\ncodex_hooks = true\n')
    roots = (home, project, system_root, executable_directory)
    before = _tree_snapshot(roots)
    _guard_doctor_side_effects(monkeypatch)

    output = _render_real_host(
        'codex',
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert output == (
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        'agent: codex\n'
        'wrapper: not found\n'
        'version: 9.9.9\n'
        'interpreter: CPython\n'
        'containment: posix\n'
        'job object configurable: not applicable\n'
        '\n'
        'configuration sources:\n'
        '- [user] $ROOT/home/.codex/hooks.json: exists\n'
        '- [user] $ROOT/home/.codex/config.toml: exists\n'
        '- [system] $ROOT/system/etc/codex/hooks.json: missing\n'
        '- [system] $ROOT/system/etc/codex/config.toml: exists\n'
        '- [project] $ROOT/project/.codex/hooks.json: exists\n'
        '- [project] $ROOT/project/.codex/config.toml: missing\n'
        '- [project] $ROOT/project/nested/.codex/hooks.json: missing\n'
        '- [project] $ROOT/project/nested/.codex/config.toml: exists\n'
        '\n'
        'registrations:\n'
        '- source: $ROOT/home/.codex/hooks.json\n'
        '  event: PostToolUse\n'
        '  matcher: ^Bash$\n'
        '  command: sequential-hooks --api-token [REDACTED]\n'
        '  timeout: unknown\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: posix_shell\n'
        '  windows launch: windows_shell\n'
        '\n'
        'findings:\n'
        '- [warning] codex_hook_trust_unknown: Codex configuration at '
        '$ROOT/home/.codex/hooks.json has a non-managed hook definition whose trust state '
        'cannot be established statically\n'
        '  correction: Review the hook with /hooks inside Codex.\n'
        '- [warning] codex_feature_alias_deprecated: Codex configuration at '
        '$ROOT/home/.codex/config.toml uses the deprecated features.codex_hooks '
        'compatibility alias\n'
        '  correction: Use features.hooks instead.\n'
        '- [warning] codex_feature_alias_deprecated: Codex configuration at '
        '$ROOT/system/etc/codex/config.toml uses the deprecated features.codex_hooks '
        'compatibility alias\n'
        '  correction: Use features.hooks instead.\n'
        '- [warning] codex_feature_alias_deprecated: Codex configuration at '
        '$ROOT/project/nested/.codex/config.toml uses the deprecated features.codex_hooks '
        'compatibility alias\n'
        '  correction: Use features.hooks instead.\n'
        '- [warning] outer_timeout_missing: outer handler timeout is missing\n'
        '  correction: Set an outer handler timeout of at least 5 seconds.\n'
        '- [warning] command_uninspectable: registration command cannot be inspected '
        'statically\n'
        '  correction: Use a loader-documented literal launch form '
        'and check the wrapper argument grammar.\n'
        '- [error] wrapper_executable_missing: wrapper executable cannot be resolved\n'
        '  correction: Install or configure a runnable wrapper executable.\n'
        '\n'
        'limitations:\n'
        '- active profiles cannot be established from the inspected configuration files\n'
        '- session overrides cannot be established from the inspected configuration files\n'
        '- plugin hooks cannot be inspected without scanning enabled plugin installations\n'
        '- persisted hook trust state cannot be established from hook definitions\n'
        '- cloud or MDM managed hooks cannot be inspected as ordinary local configuration\n'
        '- managed requirements.toml hooks and constraints cannot be established here\n'
        '- allow_managed_hooks_only can suppress user, project, session, and plugin hooks\n'
        '- local source absence does not prove the absence of an effective registration\n'
    )
    assert _tree_snapshot(roots) == before


def test_inspect_host_appends_common_findings_with_exact_corrections(
    tmp_path: Path,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    cwd.mkdir()
    source = home / '.claude/settings.json'
    document: object = {
        'hooks': {
            'PreToolUse': [
                {
                    'matcher': '^Bash$',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks',
                            'args': ['--agent', 'claude', 'child'],
                            'timeout': 2,
                        },
                        {
                            'type': 'command',
                            'command': 'child',
                            'args': list[str](),
                        },
                        {
                            'type': 'command',
                            'command': 'sequential-hooks',
                            'args': ['--agent', 'claude', 'other'],
                            'timeout': 30,
                        },
                    ],
                }
            ]
        }
    }
    _write_json(source, document)

    report = _inspect_host(
        'claude',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=None,
        path_ext=None,
    )

    codes = [finding.code for finding in report.findings]
    assert 'outer_timeout_too_small' in codes
    assert 'outer_timeout_missing' in codes
    assert 'wrapper_executable_missing' in codes
    assert 'child_executable_missing' in codes
    assert 'duplicate_wrapper' in codes
    assert 'definite_matcher_overlap' in codes
    assert 'duplicate_child_registration' in codes
    corrections = {
        finding.code: finding.correction
        for finding in report.findings
        if finding.correction is not None
    }
    assert corrections['outer_timeout_too_small'] == (
        'Set an outer handler timeout of at least 5 seconds.'
    )
    assert corrections['duplicate_wrapper'] == (
        'Keep one wrapper registration for each event and matcher.'
    )
    assert corrections['duplicate_child_registration'] == (
        'Remove the separately registered literal child.'
    )


@pytest.mark.parametrize(
    ('host', 'relative_path', 'document', 'event'),
    _DISPATCH_CASES,
)
def test_inspect_host_dispatches_each_loader(
    tmp_path: Path,
    host: str,
    relative_path: str,
    document: object,
    event: str,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    executable_directory = tmp_path / 'bin'
    executable_directory.mkdir()
    cwd.mkdir()
    suffix = '.EXE' if sys.platform == 'win32' else ''
    for name in ('child', 'sequential-hooks'):
        _create_executable(executable_directory / f'{name}{suffix}')
    _write_json(home / relative_path, document)

    report = _inspect_host(
        host,
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
    )

    assert tuple(case[0] for case in _DISPATCH_CASES) == ('claude', 'codex', 'agy')
    assert report.host == host
    assert report.executable == executable_directory / f'sequential-hooks{suffix}'
    assert report.version == '0.0.0'
    assert any(source.path == home / relative_path and source.exists for source in report.sources)
    if host == 'agy':
        assert [
            (registration.event, registration.type) for registration in report.registrations
        ] == [(event, 'command')]
    else:
        assert [
            (registration.event, registration.type) for registration in report.registrations
        ] == [(event, 'command')]


def test_inspect_host_inspects_each_registration_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    cwd.mkdir()
    hook = {
        'command': 'sequential-hooks --agent agy --event PostToolUse --step-timeout 30 child',
        'timeout': 30,
    }
    _write_json(
        home / '.gemini/config/hooks.json',
        {'sequential-hooks': {'PreToolUse': [{'hooks': [hook, hook]}]}},
    )
    monkeypatch.setattr(_doctor.sys, 'platform', 'linux')
    inspections: list[Registration] = []

    def inspect_once(registration: Registration, *, windows: bool) -> InspectedCommand:
        inspections.append(registration)
        return inspect_command(registration, windows=windows)

    monkeypatch.setattr(_doctor, 'inspect_command', inspect_once)

    report = _inspect_host(
        'agy',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(tmp_path),
        path_ext=None,
    )

    assert len(report.registrations) == 2
    codes = [finding.code for finding in report.findings]
    wrong_events = [finding for finding in report.findings if finding.code == 'wrong_event']
    assert len(wrong_events) == 2
    assert wrong_events[0] == wrong_events[1]
    assert codes.count('agy_chain_budget_uncontained') == 2
    assert codes.count('duplicate_wrapper') == 1
    assert inspections == list(report.registrations)


def test_render_report_escapes_every_dynamic_scalar() -> None:
    separator = '\\\\' if os.name == 'nt' else '/'
    source = ConfigSource(Path('/config\ninjected.json'), SourceKind.USER, exists=True)
    registration = Registration(
        source=source,
        event='PreToolUse\ninjected',
        matcher='^Bash$\rhidden',
        command='child --api-token private-value safe\x1b[31m',
        args=('--agent', 'claude', 'child'),
        launch_semantics=LaunchSemantics.DIRECT_ARGV,
        command_windows=None,
        windows_launch_semantics=LaunchSemantics.DIRECT_ARGV,
        timeout=30.0,
        enabled=True,
        type='command\ud800',
    )
    report = DoctorReport(
        host=cast('str', 'claude\ninjected'),
        executable=Path('/bin\rsequential-hooks'),
        version='0.0.0\x1b[2J',
        platform=PlatformReport(
            'CPython\ninjected',
            'posix\rhidden',
            None,
            ('platform\nlimitation',),
        ),
        sources=(source,),
        registrations=(registration,),
        findings=(
            Finding(
                FindingSeverity.WARNING,
                'unsafe\ncode',
                'unsafe\rmessage\x1b[31m',
                'unsafe\ud800correction',
            ),
        ),
        limitations=('host\x00limitation',),
    )
    stream = io.StringIO()

    _render_report(report, stream)

    assert stream.getvalue() == (
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        'agent: claude\\ninjected\n'
        f'wrapper: {separator}bin\\rsequential-hooks\n'
        'version: 0.0.0\\x1b[2J\n'
        'interpreter: CPython\\ninjected\n'
        'containment: posix\\rhidden\n'
        'job object configurable: not applicable\n'
        '\n'
        'configuration sources:\n'
        f'- [user] {separator}config\\ninjected.json: exists\n'
        '\n'
        'registrations:\n'
        f'- source: {separator}config\\ninjected.json\n'
        '  event: PreToolUse\\ninjected\n'
        '  matcher: ^Bash$\\rhidden\n'
        '  command: child --api-token [REDACTED] safe\\x1b[31m\n'
        '  timeout: 30.0\n'
        '  enabled: true\n'
        '  type: command\\ud800\n'
        '  launch: direct_argv\n'
        '  windows launch: direct_argv\n'
        '\n'
        'findings:\n'
        '- [warning] unsafe\\ncode: unsafe\\rmessage\\x1b[31m\n'
        '  correction: unsafe\\ud800correction\n'
        '\n'
        'limitations:\n'
        '- platform\\nlimitation\n'
        '- host\\x00limitation\n'
    )


def test_render_report_header_explains_ascii_backslash_escapes() -> None:
    report = DoctorReport(
        host='claude',
        executable=Path(r'C:\Tools\sequential-hooks.exe'),
        version='0.0.0',
        platform=PlatformReport('CPython', 'posix', None, ()),
        sources=(),
        registrations=(),
        findings=(),
        limitations=(),
    )
    stream = io.StringIO()

    _render_report(report, stream)

    assert stream.getvalue().splitlines()[:3] == [
        'sequential-hooks doctor',
        r'display: ASCII backslash escapes; \\ represents one literal backslash',
        'agent: claude',
    ]
    assert 'wrapper: C:\\\\Tools\\\\sequential-hooks.exe\n' in stream.getvalue()


def test_render_report_is_exact_and_redaction_safe(tmp_path: Path) -> None:
    source = ConfigSource(tmp_path / 'settings.json', SourceKind.USER, exists=True)
    report = DoctorReport(
        host='claude',
        executable=tmp_path / 'bin/sequential-hooks',
        version='0.0.0',
        platform=PlatformReport(
            'CPython',
            'posix',
            None,
            ('platform limitation',),
        ),
        sources=(
            source,
            ConfigSource(tmp_path / 'missing.json', SourceKind.PROJECT, exists=False),
        ),
        registrations=(_registration(source, 'child --api-token private-value'),),
        findings=(
            Finding(
                FindingSeverity.WARNING,
                'example_warning',
                'example problem',
                'Apply the exact correction.',
            ),
        ),
        limitations=('host limitation',),
    )
    stream = io.StringIO()

    _render_report(report, stream)

    assert stream.getvalue() == (
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        'agent: claude\n'
        f'wrapper: {_escaped_path(tmp_path / "bin/sequential-hooks")}\n'
        'version: 0.0.0\n'
        'interpreter: CPython\n'
        'containment: posix\n'
        'job object configurable: not applicable\n'
        '\n'
        'configuration sources:\n'
        f'- [user] {_escaped_path(tmp_path / "settings.json")}: exists\n'
        f'- [project] {_escaped_path(tmp_path / "missing.json")}: missing\n'
        '\n'
        'registrations:\n'
        f'- source: {_escaped_path(tmp_path / "settings.json")}\n'
        '  event: PreToolUse\n'
        '  matcher: ^Bash$\n'
        '  command: child --api-token [REDACTED]\n'
        '  timeout: 30.0\n'
        '  enabled: true\n'
        '  type: command\n'
        '  launch: direct_argv\n'
        '  windows launch: direct_argv\n'
        '\n'
        'findings:\n'
        '- [warning] example_warning: example problem\n'
        '  correction: Apply the exact correction.\n'
        '\n'
        'limitations:\n'
        '- platform limitation\n'
        '- host limitation\n'
    )
    assert 'private-value' not in stream.getvalue()
    assert repr(report.registrations[0].args) not in stream.getvalue()


def test_render_report_preserves_deep_paths_and_long_corrections() -> None:
    common = '/deep/' + ('segment/' * 90)
    first_path = Path(f'{common}first.json')
    second_path = Path(f'{common}second.json')
    correction = 'Apply ' + ('the exact nested correction ' * 30) + 'now.'
    report = DoctorReport(
        host='claude',
        executable=None,
        version='0.0.0',
        platform=PlatformReport('CPython', 'posix', None, ()),
        sources=(
            ConfigSource(first_path, SourceKind.USER, exists=True),
            ConfigSource(second_path, SourceKind.PROJECT, exists=False),
        ),
        registrations=(),
        findings=(
            Finding(
                FindingSeverity.WARNING,
                'deep_configuration',
                'deep configuration requires correction',
                correction,
            ),
        ),
        limitations=(),
    )
    stream = io.StringIO()

    _render_report(report, stream)

    output = stream.getvalue()
    assert f'- [user] {_escaped_path(first_path)}: exists\n' in output
    assert f'- [project] {_escaped_path(second_path)}: missing\n' in output
    assert first_path != second_path
    assert f'  correction: {correction}\n' in output


def test_render_report_uses_redacted_active_windows_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ConfigSource(
        Path(r'C:\Users\example\.codex\hooks.json'), SourceKind.USER, exists=True
    )
    registration = Registration(
        source=source,
        event='PreToolUse',
        matcher='^Bash$',
        command='inactive-posix-command',
        args=None,
        launch_semantics=LaunchSemantics.POSIX_SHELL,
        command_windows='active-windows-command --api-token private-value',
        windows_launch_semantics=LaunchSemantics.HOST_TOKENIZER,
        timeout=30.0,
        enabled=True,
        type='command',
    )
    report = DoctorReport(
        host='codex',
        executable=Path(r'C:\Tools\sequential-hooks.exe'),
        version='0.1.0',
        platform=PlatformReport(
            'CPython',
            'windows',
            job_object_configurable=True,
            limitations=(),
        ),
        sources=(source,),
        registrations=(registration,),
        findings=(),
        limitations=(),
    )
    stream = io.StringIO()
    monkeypatch.setattr(_doctor.sys, 'platform', 'win32')

    _render_report(report, stream)

    output = stream.getvalue()
    assert '  command: active-windows-command --api-token [REDACTED]\n' in output
    assert 'inactive-posix-command' not in output
    assert 'private-value' not in output


def test_render_report_writes_ascii_safe_code_point_escapes() -> None:
    separator = '\\\\' if os.name == 'nt' else '/'
    raw_stream = io.BytesIO()
    stream = io.TextIOWrapper(raw_stream, encoding='ascii')
    report = DoctorReport(
        host='claude',
        executable=Path('/café/工具'),
        version='é漢🙂\\literal',
        platform=PlatformReport('CPython', 'posix', None, ()),
        sources=(),
        registrations=(),
        findings=(),
        limitations=(),
    )
    try:
        _render_report(report, stream)
        stream.flush()

        lines = raw_stream.getvalue().decode('ascii').splitlines()
        assert f'wrapper: {separator}caf\\xe9{separator}\\u5de5\\u5177' in lines
        assert 'version: \\xe9\\u6f22\\U0001f642\\\\literal' in lines
    finally:
        stream.close()


@pytest.mark.parametrize(
    ('severity', 'expected_status'),
    [
        (FindingSeverity.INFO, 0),
        (FindingSeverity.WARNING, 1),
        (FindingSeverity.ERROR, 1),
    ],
)
def test_run_doctor_status_reflects_actionable_findings(
    monkeypatch: pytest.MonkeyPatch,
    severity: FindingSeverity,
    expected_status: int,
) -> None:
    report = DoctorReport(
        'claude',
        None,
        None,
        PlatformReport('CPython', 'posix', None, ()),
        (),
        (),
        (Finding(severity, 'example', 'message'),),
        ('informational limitation',),
    )

    def fake_inspect_host(*_args: object, **_kwargs: object) -> DoctorReport:
        return report

    monkeypatch.setattr('sequential_hooks._doctor._inspect_host', fake_inspect_host)
    stream = io.StringIO()

    status = run_doctor(
        'claude',
        home=Path('/home'),
        cwd=Path('/work'),
        system_root=Path('/'),
        path_value=None,
        path_ext=None,
        stream=stream,
    )

    assert status == expected_status
    assert '[info] example: message' in stream.getvalue() or severity is not FindingSeverity.INFO


def test_snapshot_normalizes_only_known_path_fields(tmp_path: Path) -> None:
    root = tmp_path / 'caf\u00e9'
    source = ConfigSource(root / 'settings.json', SourceKind.USER, exists=True)
    raw_path = str(source.path)
    report = DoctorReport(
        host='codex',
        executable=root / 'bin/sequential-hooks',
        version='0.0.0',
        platform=PlatformReport('CPython', 'posix', None, ()),
        sources=(source,),
        registrations=(_registration(source, f'child {raw_path}'),),
        findings=(
            Finding(
                FindingSeverity.WARNING,
                'source_path',
                f'Codex configuration at {raw_path} uses a compatibility alias',
            ),
            Finding(
                FindingSeverity.WARNING,
                'opaque',
                f'raw {raw_path}',
                f'Keep {raw_path}',
            ),
        ),
        limitations=(f'raw {raw_path}',),
    )
    stream = io.StringIO()
    _render_report(report, stream)

    output = _normalize_report_paths(stream.getvalue(), report, root)

    escaped = _escaped_path(source.path)
    assert '\\xe9' in escaped
    assert 'wrapper: $ROOT/bin/sequential-hooks\n' in output
    assert '- [user] $ROOT/settings.json: exists\n' in output
    assert '- source: $ROOT/settings.json\n' in output
    assert (
        '- [warning] source_path: Codex configuration at $ROOT/settings.json '
        'uses a compatibility alias\n'
    ) in output
    assert f'  command: child {escaped}\n' in output
    assert f'- [warning] opaque: raw {escaped}\n' in output
    assert f'  correction: Keep {escaped}\n' in output
    assert f'- raw {escaped}\n' in output


def test_windows_probe_resolves_real_wrapper_rejects_shim_and_never_assigns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    executable_directory = tmp_path / 'Bin'
    cwd.mkdir()
    executable_directory.mkdir()
    wrapper = executable_directory / 'sequential-hooks.EXE'
    wrapper.write_text('')
    shim = executable_directory / 'child.CMD'
    shim.write_text('')
    _write_json(
        home / '.claude/settings.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'hooks': [
                            {
                                'type': 'command',
                                'command': 'sequential-hooks',
                                'args': ['--agent', 'claude', 'child'],
                                'timeout': 30,
                            }
                        ]
                    }
                ]
            }
        },
    )
    api = _ProbeApi()
    monkeypatch.setattr(_common.sys, 'platform', 'win32')
    monkeypatch.setattr(_windows_api, 'NativeWindowsApi', lambda: api)

    report = _inspect_host(
        'claude',
        home=home,
        cwd=cwd,
        system_root=tmp_path / 'system',
        path_value=str(executable_directory),
        path_ext=None,
    )

    assert report.executable == wrapper
    assert report.platform.interpreter == 'CPython'
    assert report.platform.containment == 'windows'
    assert report.platform.job_object_configurable is True
    assert api.operations == ['create', 'close']
    unsupported = [
        finding for finding in report.findings if finding.code == 'child_executable_unsupported'
    ]
    assert len(unsupported) == 1
    assert unsupported[0].correction == (
        'Replace the Windows batch shim with a real .exe or .com executable entry point.'
    )


@pytest.mark.parametrize(
    'token',
    [
        r'\\private-server\share\sequential-hooks.exe',
        '//private-server/share/sequential-hooks.exe',
        r'\\?\C:\private-device\sequential-hooks.exe',
        r'\\.\private-device\sequential-hooks.exe',
    ],
)
@pytest.mark.parametrize('position', ['registered', 'wrapper', 'child'])
def test_windows_remote_tokens_are_unresolved_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    position: str,
) -> None:
    command = 'sequential-hooks' if position == 'child' else token
    args = ['--agent', 'claude', token if position == 'child' else 'local-child']
    if position == 'registered':
        command = token.replace('sequential-hooks.exe', 'hook.exe')
        args = []
    _write_json(
        tmp_path / '.claude/settings.json',
        {
            'hooks': {
                'PreToolUse': [
                    {
                        'hooks': [
                            {'type': 'command', 'command': command, 'args': args, 'timeout': 30},
                            {
                                'type': 'command',
                                'command': 'local-control',
                                'args': list[str](),
                                'timeout': 30,
                            },
                        ]
                    }
                ]
            }
        },
    )
    resolved: list[str] = []

    def resolve_local(executable: str, **_kwargs: object) -> ExecutableResolution:
        assert 'private-' not in executable, 'remote/device token reached filesystem lookup'
        resolved.append(executable)
        return ExecutableResolution(executable, None, ExecutableStatus.NOT_FOUND)

    monkeypatch.setattr(_doctor.sys, 'platform', 'win32')
    monkeypatch.setattr(_common, 'resolve_executable', resolve_local)
    monkeypatch.setattr(_doctor, 'resolve_executable', resolve_local)
    monkeypatch.setattr(
        _doctor,
        'inspect_platform',
        lambda: PlatformReport('CPython', 'windows', job_object_configurable=True, limitations=()),
    )
    report = _inspect_host(
        'claude',
        home=tmp_path,
        cwd=tmp_path / 'project',
        system_root=tmp_path / 'system',
        path_value='',
        path_ext='.EXE',
    )
    uninspectable = [item for item in report.findings if item.code == 'command_uninspectable']
    assert len(uninspectable) == 1
    assert uninspectable[0].severity is FindingSeverity.WARNING
    assert 'local' in (uninspectable[0].correction or '')
    assert 'local-control' in resolved
    assert 'sequential-hooks' in resolved
    if position == 'wrapper':
        assert 'local-child' in resolved
    stream = io.StringIO()
    _render_report(report, stream)
    output = stream.getvalue()
    assert 'private-' not in output
    assert token not in output
    assert 'command_uninspectable' in output
    assert 'local-control' in output
