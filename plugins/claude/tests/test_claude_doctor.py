# pyright: reportPrivateUsage=false

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.claude._sequential_hooks import _doctor as _claude
from plugins.claude._sequential_hooks._doctor import (
    _discover_sources,
    _inspection_limitations,
    _load_registrations,
)
from sequential_hooks._doctor._command import inspect_command
from sequential_hooks._doctor._common import analyze_registration, analyze_registration_pairs
from sequential_hooks._registry import load_host
from sequential_hooks.hosts import (
    Finding,
    FindingSeverity,
    JsonObject,
    JsonValue,
    LaunchSemantics,
    SourceKind,
)


def _write_settings(path: Path, hooks: JsonValue, **settings: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'hooks': hooks, 'unrelated': 'private-sentinel', **settings}))


@pytest.mark.parametrize(
    ('source_bytes', 'expected_code'),
    [
        (b'\xffprivate-byte-sentinel', 'claude_source_encoding_malformed'),
        (b'{"private": NaN}', 'claude_json_malformed'),
        (b'{"private": Infinity}', 'claude_json_malformed'),
        (b'{"private": -Infinity}', 'claude_json_malformed'),
        (f'{{"private": {"9" * 4301}}}'.encode(), 'claude_json_malformed'),
        (b'[]', 'claude_settings_malformed'),
        ((b'{"private":' * 1_500) + b'{}' + (b'}' * 1_500), 'claude_json_malformed'),
        (
            (
                '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command",'
                '"command":"private-command",'
                f'"timeout":{"9" * 401}'
                '}] }]}}'
            ).encode(),
            'claude_timeout_malformed',
        ),
    ],
    ids=[
        'invalid-utf8',
        'nan',
        'infinity',
        'negative-infinity',
        'integer-limit',
        'non-object',
        'nested-object',
        'timeout-overflow',
    ],
)
def test_bad_json_source_is_isolated_from_later_valid_source(
    tmp_path: Path,
    source_bytes: bytes,
    expected_code: str,
) -> None:
    user = tmp_path / '.claude/settings.json'
    project = tmp_path / 'project/.claude/settings.json'
    user.parent.mkdir(parents=True)
    user.write_bytes(source_bytes)
    _write_settings(
        project,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe-command'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe-command']
    assert expected_code in {finding.code for finding in findings}
    rendered = ' '.join(finding.message for finding in findings)
    assert 'private-byte-sentinel' not in rendered
    assert 'private-command' not in rendered


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize('direct', [False, True])
def test_conflicting_project_values_make_only_non_managed_state_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    direct: bool,
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    root = tmp_path / 'repo'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    (root / '.git').mkdir()
    _write_settings(root / '.claude/settings.json', {}, disableAllHooks=True)
    _write_settings(nested / '.claude/settings.json', {}, disableAllHooks=False)
    local_hook: JsonObject = {'type': 'command', 'command': 'local'}
    if direct:
        local_hook['args'] = []
    _write_settings(
        root / '.claude/settings.local.json',
        {'PreToolUse': [{'hooks': [local_hook]}]},
    )
    _write_settings(
        tmp_path / 'system/etc/claude-code/managed-settings.json',
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'managed', 'args': []}]}]},
    )

    registrations, _findings = _load_registrations(
        _discover_sources(tmp_path / 'home', nested, tmp_path / 'system')
    )

    assert [(item.command, item.enabled) for item in registrations] == [
        ('local', None),
        ('managed', True),
    ]

    registration = registrations[0]
    findings = (
        *registration.configuration_findings,
        *analyze_registration(
            load_host('claude'),
            (registration, inspect_command(registration, windows=False)),
            path_value='',
            path_ext=None,
            windows=False,
        ),
    )
    unknowns = [finding for finding in findings if finding.code == 'enabled_unknown']
    assert len(unknowns) == 1
    unknown = unknowns[0]
    assert unknown.severity is FindingSeverity.WARNING
    assert [finding.code for finding in findings] == [
        *(['claude_exec_form_recommended'] if not direct else []),
        'enabled_unknown',
        'outer_timeout_missing',
        'child_executable_missing',
    ]
    assert unknown.correction is not None
    assert 'disableAllHooks' in unknown.correction
    assert 'settings' in unknown.correction
    assert 'enabled' not in unknown.correction
    assert len(unknown.correction) <= 160


@pytest.mark.parametrize(
    ('settings', 'expected_user', 'expected_managed'),
    [
        ({}, True, True),
        ({'user': True}, False, True),
        ({'user': False, 'project': True}, False, True),
        ({'project': True, 'local': False}, True, True),
        ({'local': True, 'managed': False}, True, True),
        ({'managed': True}, False, False),
    ],
)
def test_disable_all_hooks_uses_visible_scope_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, bool],
    expected_user: bool,
    expected_managed: bool,
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.git').mkdir()
    system_root = tmp_path / 'system'
    paths = {
        'user': tmp_path / 'home/.claude/settings.json',
        'project': root / '.claude/settings.json',
        'local': root / '.claude/settings.local.json',
        'managed': system_root / 'etc/claude-code/managed-settings.json',
    }
    for scope, path in paths.items():
        command = 'managed-child' if scope == 'managed' else f'{scope}-child'
        _write_settings(
            path,
            {'PreToolUse': [{'hooks': [{'type': 'command', 'command': command, 'args': []}]}]},
            **({'disableAllHooks': settings[scope]} if scope in settings else {}),
        )

    registrations, _findings = _load_registrations(
        _discover_sources(tmp_path / 'home', root, system_root)
    )

    by_command = {registration.command: registration.enabled for registration in registrations}
    assert by_command['user-child'] is expected_user
    assert by_command['project-child'] is expected_user
    assert by_command['local-child'] is expected_user
    assert by_command['managed-child'] is expected_managed


def test_discover_sources_deduplicates_home_project_convergence(tmp_path: Path) -> None:
    git_root = tmp_path
    (git_root / '.git').mkdir()
    settings = git_root / '.claude/settings.json'
    _write_settings(
        settings,
        {
            'PreToolUse': [
                {
                    'matcher': 'Bash',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks --agent claude child',
                        }
                    ],
                }
            ]
        },
    )

    sources = _discover_sources(git_root, git_root, tmp_path / 'sys')

    paths = [source.path for source in sources]
    assert len(paths) == len(set(paths))
    retained = [source for source in sources if source.path == settings]
    assert len(retained) == 1
    assert retained[0].kind is SourceKind.USER

    registrations, loader_findings = _load_registrations(sources)
    findings = (
        *loader_findings,
        *analyze_registration_pairs(
            (
                (registration, inspect_command(registration, windows=False))
                for registration in registrations
            ),
            windows=False,
        ),
    )
    codes = {finding.code for finding in findings}
    assert len(registrations) == 1
    assert 'duplicate_wrapper' not in codes
    assert 'definite_matcher_overlap' not in codes


def test_discover_sources_in_documented_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / 'home'
    root = tmp_path / 'repo'
    cwd = root / 'nested'
    system_root = tmp_path / 'system'
    cwd.mkdir(parents=True)
    (root / '.git').mkdir()
    user = home / '.claude/settings.json'
    project = root / '.claude/settings.json'
    local = root / '.claude/settings.local.json'
    managed = (
        system_root / 'Library' / 'Application Support' / 'ClaudeCode' / 'managed-settings.json'
    )
    drop_in_directory = managed.parent / 'managed-settings.d'
    for path in (user, project, local, managed):
        _write_settings(path, {})
    _write_settings(drop_in_directory / 'b.json', {})
    _write_settings(drop_in_directory / 'a.json', {})
    _write_settings(drop_in_directory / '.hidden.json', {})
    (drop_in_directory / 'not-json.txt').write_text('{}')
    monkeypatch.setattr(_claude.sys, 'platform', 'darwin')

    sources = _discover_sources(home, cwd, system_root)

    assert [(source.path, source.kind, source.exists) for source in sources] == [
        (user, SourceKind.USER, True),
        (project, SourceKind.PROJECT, True),
        (local, SourceKind.LOCAL, True),
        (cwd / '.claude/settings.json', SourceKind.PROJECT, False),
        (cwd / '.claude/settings.local.json', SourceKind.LOCAL, False),
        (managed, SourceKind.MANAGED, True),
        (drop_in_directory / 'a.json', SourceKind.MANAGED, True),
        (drop_in_directory / 'b.json', SourceKind.MANAGED, True),
    ]


@pytest.mark.parametrize(
    ('platform_name', 'parts'),
    [
        ('linux', ('etc', 'claude-code', 'managed-settings.json')),
        ('win32', ('Program Files', 'ClaudeCode', 'managed-settings.json')),
    ],
)
def test_discover_sources_selects_current_managed_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    parts: tuple[str, ...],
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', platform_name)

    sources = _discover_sources(tmp_path / 'home', tmp_path / 'cwd', tmp_path / 'system')

    assert sources[-1].path == tmp_path / 'system' / Path(*parts)
    assert sources[-1].kind is SourceKind.MANAGED
    assert sources[-1].exists is False


@pytest.mark.parametrize('failure_operation', ['stat', 'iterdir'])
def test_drop_in_enumeration_failure_reports_unreadable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_operation: str,
) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'project'
    system_root = tmp_path / 'system'
    drop_in_directory = system_root / 'etc/claude-code/managed-settings.d'
    drop_in_directory.mkdir(parents=True)
    original_iterdir = Path.iterdir
    original_stat = Path.stat

    def fail_drop_in_enumeration(path: Path) -> object:
        if failure_operation == 'iterdir' and path == drop_in_directory:
            raise OSError('private-sentinel')
        return original_iterdir(path)

    def fail_drop_in_stat(path: Path, *args: object, **kwargs: object) -> object:
        if failure_operation == 'stat' and path == drop_in_directory:
            raise OSError('private-sentinel')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'iterdir', fail_drop_in_enumeration)
    monkeypatch.setattr(Path, 'stat', fail_drop_in_stat)

    sources = _discover_sources(home, cwd, system_root)
    registrations, findings = _load_registrations(sources)

    assert [source.path for source in sources] == [
        home / '.claude/settings.json',
        cwd / '.claude/settings.json',
        cwd / '.claude/settings.local.json',
        system_root / 'etc/claude-code/managed-settings.json',
        drop_in_directory,
    ]
    assert registrations == ()
    assert [finding.code for finding in findings] == ['claude_source_unreadable']
    assert 'private-sentinel' not in findings[0].message


def test_empty_managed_drop_in_directory_adds_no_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    system_root = tmp_path / 'system'
    drop_in_directory = system_root / 'etc/claude-code/managed-settings.d'
    drop_in_directory.mkdir(parents=True)

    sources = _discover_sources(tmp_path / 'home', tmp_path / 'work', system_root)

    paths = {source.path for source in sources}
    assert drop_in_directory not in paths
    assert system_root / 'etc/claude-code/managed-settings.json' in paths


def test_entry_enabled_field_has_no_activation_meaning(tmp_path: Path) -> None:
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'child',
                            'args': [],
                            'enabled': None,
                        }
                    ]
                }
            ]
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(item.command, item.enabled) for item in registrations] == [('child', True)]
    assert findings == ()


def test_every_settings_source_is_read_as_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = tmp_path / '.claude/settings.json'
    project = tmp_path / 'project/.claude/settings.json'
    _write_settings(user, {})
    _write_settings(project, {})
    calls: list[tuple[Path, str | None]] = []
    original_read_text = Path.read_text

    def read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        calls.append((path, encoding))
        assert encoding == 'utf-8'
        return original_read_text(path, encoding, errors)

    monkeypatch.setattr(Path, 'read_text', read_text)

    _load_registrations(_discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system'))

    assert calls == [(user, 'utf-8'), (project, 'utf-8')]


def test_limitations_cover_non_file_and_plugin_sources() -> None:
    limitations = _inspection_limitations()

    assert any('server-managed' in limitation for limitation in limitations)
    assert any('managed preferences' in limitation for limitation in limitations)
    assert any('registry' in limitation for limitation in limitations)
    assert any('plugin-provided' in limitation for limitation in limitations)


def test_load_registrations_preserves_native_shape_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude, 'sys', SimpleNamespace(platform='linux'))
    source = tmp_path / '.claude/settings.json'
    hooks: JsonValue = {
        'PreToolUse': [
            {
                'matcher': 'Write|Edit',
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'sequential-hooks',
                        'args': ['--agent', 'claude', 'first'],
                        'timeout': 30,
                        'enabled': False,
                    },
                    {
                        'type': 'command',
                        'command': 'sequential-hooks --agent claude second',
                        'timeout': 45.5,
                    },
                ],
            },
            {
                'hooks': [
                    {
                        'type': 'command',
                        'command': 'third',
                        'args': [],
                        'enabled': True,
                    }
                ]
            },
        ],
        'PostToolUse': [
            {
                'matcher': '*',
                'hooks': [{'type': 'command', 'command': 'fourth'}],
            }
        ],
    }
    _write_settings(
        source,
        hooks,
    )
    source_config = _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')[0]

    registrations, findings = _load_registrations((source_config,))

    assert [
        (
            registration.event,
            registration.matcher,
            registration.command,
            registration.args,
            registration.launch_semantics,
            registration.windows_launch_semantics,
            registration.timeout,
            registration.enabled,
            registration.type,
        )
        for registration in registrations
    ] == [
        (
            'PreToolUse',
            'Write|Edit',
            'sequential-hooks',
            ('--agent', 'claude', 'first'),
            LaunchSemantics.DIRECT_ARGV,
            LaunchSemantics.DIRECT_ARGV,
            30.0,
            True,
            'command',
        ),
        (
            'PreToolUse',
            'Write|Edit',
            'sequential-hooks --agent claude second',
            None,
            LaunchSemantics.POSIX_SHELL,
            LaunchSemantics.HOST_TOKENIZER,
            45.5,
            True,
            'command',
        ),
        (
            'PreToolUse',
            None,
            'third',
            (),
            LaunchSemantics.DIRECT_ARGV,
            LaunchSemantics.DIRECT_ARGV,
            None,
            True,
            'command',
        ),
        (
            'PostToolUse',
            '*',
            'fourth',
            None,
            LaunchSemantics.POSIX_SHELL,
            LaunchSemantics.HOST_TOKENIZER,
            None,
            True,
            'command',
        ),
    ]
    assert findings == ()
    assert [
        [finding.code for finding in registration.configuration_findings]
        for registration in registrations
    ] == [[], ['claude_exec_form_recommended'], [], ['claude_exec_form_recommended']]


@pytest.mark.parametrize('managed', [False, True])
def test_malformed_disable_all_hooks_matches_source_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    source = (
        tmp_path / 'system/etc/claude-code/managed-settings.json'
        if managed
        else tmp_path / 'home/.claude/settings.json'
    )
    _write_settings(
        source,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'child', 'args': []}]}]},
        disableAllHooks='private-invalid-value',
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path / 'home', tmp_path / 'work', tmp_path / 'system')
    )

    assert [item.command for item in registrations] == (['child'] if managed else [])
    assert [finding.code for finding in findings] == ['claude_disable_all_hooks_malformed']
    assert 'private-invalid-value' not in findings[0].message


def test_malformed_sources_are_isolated_and_values_are_not_rendered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude, 'sys', SimpleNamespace(platform='linux'))
    user = tmp_path / '.claude/settings.json'
    project = tmp_path / 'project/.claude/settings.json'
    user.parent.mkdir(parents=True)
    user.write_text('{"hooks": {"PreToolUse": []}, "hooks": {"private-sentinel": "secret"}}')
    _write_settings(
        project,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe-command'}]}]},
    )
    sources = _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')

    registrations, findings = _load_registrations(sources)

    assert [registration.command for registration in registrations] == ['safe-command']
    assert [finding.code for finding in findings] == ['claude_json_malformed']
    assert [finding.code for finding in registrations[0].configuration_findings] == [
        'claude_exec_form_recommended',
    ]
    assert all('private-sentinel' not in finding.message for finding in findings)
    assert all('secret' not in finding.message for finding in findings)


def test_managed_drop_ins_override_the_base_in_filename_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude.sys, 'platform', 'linux')
    managed = tmp_path / 'system/etc/claude-code'
    hooks: JsonValue = {
        'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'managed', 'args': []}]}]
    }
    _write_settings(managed / 'managed-settings.json', hooks, disableAllHooks=True)
    _write_settings(managed / 'managed-settings.d/20-enable.json', {}, disableAllHooks=False)
    _write_settings(managed / 'managed-settings.d/10-disable.json', {}, disableAllHooks=True)

    registrations, _findings = _load_registrations(
        _discover_sources(tmp_path / 'home', tmp_path / 'work', tmp_path / 'system')
    )

    assert [(item.command, item.enabled) for item in registrations] == [('managed', True)]


def test_missing_sources_are_retained(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.git').mkdir()
    sources = _discover_sources(tmp_path / 'home', project, tmp_path / 'system')

    assert sources
    assert all(source.exists is False for source in sources)


@pytest.mark.parametrize('timeout', [-2, 0])
def test_nonpositive_timeout_survives_loading_for_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: int,
) -> None:
    monkeypatch.setattr(_claude, 'sys', SimpleNamespace(platform='linux'))
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'child', 'timeout': timeout}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].timeout == float(timeout)
    assert findings == ()
    assert [finding.code for finding in registrations[0].configuration_findings] == [
        'claude_exec_form_recommended',
    ]


def test_posix_command_string_recommends_exec_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude, 'sys', SimpleNamespace(platform='linux'))
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'child'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].launch_semantics is LaunchSemantics.POSIX_SHELL
    assert findings == ()
    assert registrations[0].configuration_findings == (
        Finding(
            FindingSeverity.INFO,
            'claude_exec_form_recommended',
            'Claude Code command strings use a POSIX shell',
            'Use the exec form with explicit args.',
        ),
    )


@pytest.mark.parametrize(
    ('field', 'expected_code'),
    [
        ('args', 'claude_args_malformed'),
        ('timeout', 'claude_timeout_malformed'),
    ],
)
def test_present_null_hook_field_is_malformed(
    tmp_path: Path,
    field: str,
    expected_code: str,
) -> None:
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {
            'PreToolUse': [
                {'hooks': [{'type': 'command', 'command': 'private-command', field: None}]}
            ]
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert [finding.code for finding in findings] == [expected_code]
    assert 'private-command' not in findings[0].message


def test_present_null_matcher_is_malformed(tmp_path: Path) -> None:
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {
            'PreToolUse': [
                {
                    'matcher': None,
                    'hooks': [{'type': 'command', 'command': 'private-command'}],
                }
            ]
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert [finding.code for finding in findings] == ['claude_matcher_malformed']
    assert 'private-command' not in findings[0].message


@pytest.mark.usefixtures('distribution_root')
def test_stop_wrapper_reports_only_unservable_native_event(tmp_path: Path) -> None:
    source = tmp_path / '.claude/settings.json'
    _write_settings(
        source,
        {
            'Stop': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks --agent claude child',
                        },
                        {'type': 'command', 'command': 'third-party'},
                    ]
                }
            ]
        },
    )

    registrations, loader_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    findings = (
        *loader_findings,
        *(
            finding
            for registration in registrations
            for finding in analyze_registration(
                load_host('claude'),
                (registration, inspect_command(registration, windows=False)),
                path_value=None,
                path_ext=None,
                windows=False,
            )
        ),
    )

    matching = [finding for finding in findings if finding.code == 'native_event_unsupported']
    assert len(matching) == 1
    assert matching[0].severity is FindingSeverity.ERROR
    assert {'event_missing', 'wrong_event', 'event_forbidden'}.isdisjoint(
        finding.code for finding in findings
    )


def test_structural_errors_ignore_unrelated_settings_values(tmp_path: Path) -> None:
    source = tmp_path / '.claude/settings.json'
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                'hooks': {
                    'PreToolUse': [
                        {
                            'matcher': 42,
                            'hooks': [
                                {
                                    'type': 'prompt',
                                    'command': 'private-command',
                                    'enabled': 'private-enabled',
                                }
                            ],
                        }
                    ]
                },
                'apiKey': 'private-api-key',
            }
        )
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert findings
    rendered = ' '.join(finding.message for finding in findings)
    assert 'private-command' not in rendered
    assert 'private-enabled' not in rendered
    assert 'private-api-key' not in rendered


def test_unreadable_and_malformed_layers_do_not_hide_other_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_claude, 'sys', SimpleNamespace(platform='linux'))
    user = tmp_path / '.claude/settings.json'
    project = tmp_path / 'project/.claude/settings.json'
    _write_settings(user, {'PreToolUse': 'private-sentinel'})
    _write_settings(
        project,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe'}]}]},
    )
    original_read_text = Path.read_text

    def fail_user_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == user:
            raise OSError('private-sentinel')
        return original_read_text(path, encoding, errors)

    monkeypatch.setattr(Path, 'read_text', fail_user_read)

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert [finding.code for finding in findings] == ['claude_source_unreadable']
    assert [finding.code for finding in registrations[0].configuration_findings] == [
        'claude_exec_form_recommended',
    ]
    assert all('private-sentinel' not in finding.message for finding in findings)


def test_windows_command_string_reports_conditional_shell_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / '.claude/settings.json'
    monkeypatch.setattr(_claude.sys, 'platform', 'win32')
    _write_settings(
        source,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'child'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].windows_launch_semantics is LaunchSemantics.HOST_TOKENIZER
    assert findings == ()
    notes = registrations[0].configuration_findings
    assert notes == (
        Finding(
            FindingSeverity.INFO,
            'claude_windows_shell_conditional',
            'Claude Code command strings use Git Bash on Windows when statically '
            'established as available, otherwise PowerShell; shell availability was not probed',
            'Use the exec form with explicit args, especially on Windows.',
        ),
    )
    assert any(
        'Git Bash' in finding.message and 'PowerShell' in finding.message for finding in notes
    )
    assert any('explicit args' in (finding.correction or '') for finding in notes)
