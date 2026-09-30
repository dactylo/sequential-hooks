# pyright: reportPrivateUsage=false

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from plugins.codex._sequential_hooks import _doctor as _codex
from plugins.codex._sequential_hooks._doctor import (
    _discover_sources,
    _inspection_limitations,
    _load_registrations,
)
from sequential_hooks._doctor._command import inspect_command
from sequential_hooks._doctor._common import analyze_registration, analyze_registration_pairs
from sequential_hooks._registry import load_host
from sequential_hooks.hosts import Finding, FindingSeverity, LaunchSemantics, SourceKind

_MALFORMED_HOOK_CASES = cast(
    'tuple[tuple[object, str], ...]',
    (
        ([], 'codex_hooks_malformed'),
        ({'PreToolUse': {}}, 'codex_event_malformed'),
        ({'PreToolUse': [None]}, 'codex_matcher_group_malformed'),
        (
            {'PreToolUse': [{'matcher': None, 'hooks': []}]},
            'codex_matcher_malformed',
        ),
        ({'PreToolUse': [{}]}, 'codex_handler_list_malformed'),
    ),
)


def _write_json(path: Path, hooks: object, **settings: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'hooks': hooks, **settings}))


def test_args_do_not_change_codex_shell_launch_semantics(tmp_path: Path) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks --agent codex child',
                            'args': ['ignored-as-launch-selector'],
                        }
                    ]
                }
            ]
        },
    )

    registrations, _findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].args == ('ignored-as-launch-selector',)
    assert registrations[0].launch_semantics is LaunchSemantics.POSIX_SHELL
    assert registrations[0].windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL


@pytest.mark.parametrize(
    ('source_bytes', 'expected_code'),
    [
        (b'{"private": 1, "private": 2}', 'codex_json_malformed'),
        (b'{"private": NaN}', 'codex_json_malformed'),
        (b'{"private": Infinity}', 'codex_json_malformed'),
        (f'{{"private": {"9" * 4301}}}'.encode(), 'codex_json_malformed'),
        (b'[]', 'codex_settings_malformed'),
        ((b'{"private":' * 1_500) + b'{}' + (b'}' * 1_500), 'codex_json_malformed'),
        (
            (
                '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command",'
                '"command":"private-command",'
                f'"timeout":{"9" * 401}'
                '}] }]}}'
            ).encode(),
            'codex_timeout_malformed',
        ),
    ],
    ids=[
        'duplicate-key',
        'nan',
        'infinity',
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
    user_json = tmp_path / '.codex/hooks.json'
    project_json = tmp_path / 'project/.codex/hooks.json'
    user_json.parent.mkdir(parents=True)
    user_json.write_bytes(source_bytes)
    _write_json(
        project_json,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert expected_code in {finding.code for finding in findings}
    if expected_code == 'codex_timeout_malformed':
        assert [finding.code for finding in findings] == [
            'codex_timeout_malformed',
            'codex_project_trust_unknown',
        ]
    rendered = ' '.join(finding.message for finding in findings)
    assert 'private-command' not in rendered


def test_conflicting_toml_windows_aliases_are_malformed(tmp_path: Path) -> None:
    source = tmp_path / '.codex/config.toml'
    source.parent.mkdir(parents=True)
    source.write_text(
        """
[[hooks.PreToolUse]]
[[hooks.PreToolUse.hooks]]
type = "command"
command = "private-command"
command_windows = "private-snake"
commandWindows = "private-camel"
"""
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert [finding.code for finding in findings] == ['codex_windows_command_ambiguous']
    assert all('private-command' not in finding.message for finding in findings)
    assert all('private-snake' not in finding.message for finding in findings)
    assert all('private-camel' not in finding.message for finding in findings)


def test_discover_sources_deduplicates_home_project_convergence(tmp_path: Path) -> None:
    git_root = tmp_path
    (git_root / '.git').mkdir()
    settings = git_root / '.codex/hooks.json'
    _write_json(
        settings,
        {
            'PreToolUse': [
                {
                    'matcher': 'Bash',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks --agent codex child',
                        }
                    ],
                }
            ]
        },
    )

    sources = _discover_sources(git_root, git_root, tmp_path / 'sys')

    paths = [source.path for source in sources]
    assert len(paths) == len(set(paths))
    for filename in ('hooks.json', 'config.toml'):
        path = git_root / '.codex' / filename
        retained = [source for source in sources if source.path == path]
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


def test_discover_sources_in_documented_order(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    root = tmp_path / 'repo'
    cwd = root / 'nested'
    system_root = tmp_path / 'system'
    cwd.mkdir(parents=True)
    (root / '.git').mkdir()
    expected = [
        (home / '.codex/hooks.json', SourceKind.USER),
        (home / '.codex/config.toml', SourceKind.USER),
        (system_root / 'etc/codex/hooks.json', SourceKind.SYSTEM),
        (system_root / 'etc/codex/config.toml', SourceKind.SYSTEM),
        (root / '.codex/hooks.json', SourceKind.PROJECT),
        (root / '.codex/config.toml', SourceKind.PROJECT),
        (cwd / '.codex/hooks.json', SourceKind.PROJECT),
        (cwd / '.codex/config.toml', SourceKind.PROJECT),
    ]
    for path, _ in expected[::2]:
        _write_json(path, {})
    for path, _ in expected[1::2]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')

    sources = _discover_sources(home, cwd, system_root)

    assert [(source.path, source.kind, source.exists) for source in sources] == [
        (path, kind, True) for path, kind in expected
    ]


def test_feature_flags_set_registration_state_without_inferring_trust(tmp_path: Path) -> None:
    canonical = tmp_path / '.codex/config.toml'
    deprecated = tmp_path / 'project/.codex/config.toml'
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        """
[features]
hooks = false
codex_hooks = true

[[hooks.PreToolUse]]
[[hooks.PreToolUse.hooks]]
type = "command"
command = "canonical-disabled"
"""
    )
    deprecated.parent.mkdir(parents=True)
    deprecated.write_text(
        """
[features]
codex_hooks = false

[[hooks.PostToolUse]]
[[hooks.PostToolUse.hooks]]
type = "command"
command = "alias-disabled"
"""
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(registration.command, registration.enabled) for registration in registrations] == [
        ('canonical-disabled', False),
        ('alias-disabled', False),
    ]
    assert [finding.code for finding in findings] == [
        'codex_feature_alias_deprecated',
        'codex_hook_trust_unknown',
        'codex_feature_alias_deprecated',
        'codex_project_trust_unknown',
    ]


def test_invalid_utf8_source_is_isolated_from_later_valid_source(tmp_path: Path) -> None:
    user_json = tmp_path / '.codex/hooks.json'
    project_json = tmp_path / 'project/.codex/hooks.json'
    user_json.parent.mkdir(parents=True)
    user_json.write_bytes(b'\xffprivate-byte-sentinel')
    _write_json(
        project_json,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert [finding.code for finding in findings] == [
        'codex_source_encoding_malformed',
        'codex_project_trust_unknown',
    ]
    assert all('private-byte-sentinel' not in finding.message for finding in findings)


def test_json_and_toml_sources_are_additive_and_preserve_source_order(tmp_path: Path) -> None:
    hooks_json = tmp_path / '.codex/hooks.json'
    config_toml = tmp_path / '.codex/config.toml'
    _write_json(
        hooks_json,
        {
            'PreToolUse': [
                {
                    'matcher': '^Bash$',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks',
                            'args': ['--agent', 'codex', 'first'],
                            'timeout': 30,
                            'commandWindows': 'sequential-hooks.exe --agent codex first',
                        }
                    ],
                }
            ]
        },
    )
    config_toml.write_text(
        """
[[hooks.PostToolUse]]
matcher = "^Bash$"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "sequential-hooks --agent codex second"
timeout = 45.5
"""
    )
    sources = _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')

    registrations, findings = _load_registrations(sources)

    assert [
        (
            registration.source.path,
            registration.event,
            registration.matcher,
            registration.command,
            registration.args,
            registration.launch_semantics,
            registration.command_windows,
            registration.windows_launch_semantics,
            registration.timeout,
            registration.enabled,
            registration.type,
        )
        for registration in registrations
    ] == [
        (
            hooks_json,
            'PreToolUse',
            '^Bash$',
            'sequential-hooks',
            ('--agent', 'codex', 'first'),
            LaunchSemantics.POSIX_SHELL,
            'sequential-hooks.exe --agent codex first',
            LaunchSemantics.WINDOWS_SHELL,
            30.0,
            True,
            'command',
        ),
        (
            config_toml,
            'PostToolUse',
            '^Bash$',
            'sequential-hooks --agent codex second',
            None,
            LaunchSemantics.POSIX_SHELL,
            None,
            LaunchSemantics.WINDOWS_SHELL,
            45.5,
            True,
            'command',
        ),
    ]
    assert [finding.code for finding in findings] == [
        'codex_hook_trust_unknown',
        'codex_hook_trust_unknown',
    ]
    assert all(
        finding.correction == 'Review the hook with /hooks inside Codex.' for finding in findings
    )


def test_limitations_cover_uninspectable_active_sources_and_gates() -> None:
    limitations = _inspection_limitations()

    assert any('active profiles' in limitation for limitation in limitations)
    assert any('session overrides' in limitation for limitation in limitations)
    assert any('plugin hooks' in limitation for limitation in limitations)
    assert any('trust state' in limitation for limitation in limitations)
    assert any('cloud or MDM' in limitation for limitation in limitations)
    assert any('requirements.toml' in limitation for limitation in limitations)
    assert any('allow_managed_hooks_only' in limitation for limitation in limitations)
    assert any(
        'absence' in limitation and 'effective registration' in limitation
        for limitation in limitations
    )


def test_loading_is_process_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / '.codex/hooks.json'
    marker = tmp_path / 'marker'
    _write_json(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': f'touch {marker}',
                            'args': [f'; touch {marker}'],
                        }
                    ]
                }
            ]
        },
    )

    def fail_process(*_: object, **__: object) -> object:
        raise AssertionError('configuration discovery started a process')

    monkeypatch.setattr(subprocess, 'Popen', fail_process)
    monkeypatch.setattr(subprocess, 'run', fail_process)

    registrations, _ = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert len(registrations) == 1
    assert not marker.exists()


@pytest.mark.parametrize(
    ('hook', 'expected_code'),
    [
        ({'type': 'command'}, 'codex_command_malformed'),
        ({'type': 'command', 'command': ''}, 'codex_command_malformed'),
        ({'type': 'command', 'command': 'private', 'args': None}, 'codex_args_malformed'),
        ({'type': 'command', 'command': 'private', 'timeout': True}, 'codex_timeout_malformed'),
        (
            {'type': 'command', 'command': 'private', 'timeout': 10**400},
            'codex_timeout_malformed',
        ),
        ({'type': 'command', 'command': 'private', 'async': None}, 'codex_async_malformed'),
        (
            {'type': 'command', 'command': 'private', 'commandWindows': 42},
            'codex_windows_command_malformed',
        ),
    ],
)
def test_malformed_handler_fields_are_isolated(
    tmp_path: Path,
    hook: dict[str, object],
    expected_code: str,
) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(source, {'PreToolUse': [{'hooks': [hook]}]})

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert [finding.code for finding in findings] == [expected_code]
    assert all('private-command' not in finding.message for finding in findings)


@pytest.mark.parametrize(
    ('hooks', 'expected_code'),
    _MALFORMED_HOOK_CASES,
)
def test_malformed_hook_layers_are_reported_without_values(
    tmp_path: Path,
    hooks: object,
    expected_code: str,
) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(source, hooks, unrelated='private-sentinel')

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert [finding.code for finding in findings] == [expected_code]
    assert 'private-sentinel' not in findings[0].message


def test_malformed_sources_are_isolated_and_values_are_not_rendered(tmp_path: Path) -> None:
    user_json = tmp_path / '.codex/hooks.json'
    user_toml = tmp_path / '.codex/config.toml'
    project_json = tmp_path / 'project/.codex/hooks.json'
    user_json.parent.mkdir(parents=True)
    user_json.write_text('{"hooks": {"PreToolUse": []}, "hooks": {"private-sentinel": "secret"}}')
    user_toml.write_text('[hooks\nprivate-sentinel = "secret"')
    _write_json(
        project_json,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert [finding.code for finding in findings] == [
        'codex_json_malformed',
        'codex_toml_malformed',
        'codex_project_trust_unknown',
    ]
    assert all('private-sentinel' not in finding.message for finding in findings)
    assert all('secret' not in finding.message for finding in findings)


def test_missing_sources_are_retained(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.git').mkdir()
    system_root = tmp_path / 'system'

    sources = _discover_sources(tmp_path / 'home', project, system_root)

    assert sources
    assert all(source.exists is False for source in sources)
    assert [source.path for source in sources if source.kind is SourceKind.SYSTEM] == [
        system_root / 'etc/codex/hooks.json',
        system_root / 'etc/codex/config.toml',
    ]


@pytest.mark.parametrize('timeout', [-2, 0])
def test_nonpositive_timeout_survives_loading_for_diagnosis(
    tmp_path: Path,
    timeout: int,
) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(
        source,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'child', 'timeout': timeout}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].timeout == float(timeout)
    assert [finding.code for finding in findings] == ['codex_hook_trust_unknown']


def test_oversized_toml_integer_is_isolated_from_later_valid_source(tmp_path: Path) -> None:
    user_toml = tmp_path / '.codex/config.toml'
    project_json = tmp_path / 'project/.codex/hooks.json'
    user_toml.parent.mkdir(parents=True)
    user_toml.write_text(f'sensitive_sentinel = {"9" * 4301}')
    _write_json(
        project_json,
        {'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'safe'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert [finding.code for finding in findings] == [
        'codex_toml_malformed',
        'codex_project_trust_unknown',
    ]
    assert all('sensitive_sentinel' not in finding.message for finding in findings)


def test_posix_only_command_is_diagnosed_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': "sh -c 'sequential-hooks --agent codex child'",
                        }
                    ]
                }
            ]
        },
    )
    monkeypatch.setattr(_codex.sys, 'platform', 'win32')

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].launch_semantics is LaunchSemantics.POSIX_SHELL
    assert registrations[0].windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL
    assert [finding.code for finding in findings] == ['codex_hook_trust_unknown']
    notes = registrations[0].configuration_findings
    assert notes == (
        Finding(
            FindingSeverity.WARNING,
            'codex_windows_posix_command',
            f'Codex configuration at {source} contains a command that explicitly depends on '
            'POSIX shell tokenization on Windows',
            'Add commandWindows and review the hook with /hooks inside Codex.',
        ),
    )
    assert (
        notes[0].correction == 'Add commandWindows and review the hook with /hooks inside Codex.'
    )


def test_project_registration_reports_trust_as_unresolved(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    cwd = root / 'nested'
    cwd.mkdir(parents=True)
    (root / '.git').mkdir()
    _write_json(
        root / '.codex/hooks.json',
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'project-hook'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path / 'home', cwd, tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['project-hook']
    assert [finding.code for finding in findings] == ['codex_project_trust_unknown']
    assert findings[0].correction == 'Review the hook with /hooks inside Codex.'
    assert 'trusted' not in findings[0].message.casefold()
    assert 'trust state cannot be established' in findings[0].message


def test_same_layer_malformed_toml_keeps_json_with_unknown_feature_state(
    tmp_path: Path,
) -> None:
    hooks_json = tmp_path / '.codex/hooks.json'
    config_toml = tmp_path / '.codex/config.toml'
    _write_json(
        hooks_json,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'json-hook'}]}]},
    )
    config_toml.write_text('[features')

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(registration.command, registration.enabled) for registration in registrations] == [
        ('json-hook', None)
    ]
    assert [finding.code for finding in findings] == [
        'codex_hook_trust_unknown',
        'codex_toml_malformed',
    ]


def test_same_layer_toml_feature_disables_json_hooks(tmp_path: Path) -> None:
    hooks_json = tmp_path / '.codex/hooks.json'
    config_toml = tmp_path / '.codex/config.toml'
    _write_json(
        hooks_json,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'json-hook'}]}]},
    )
    config_toml.write_text(
        """
[features]
hooks = false
"""
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(registration.command, registration.enabled) for registration in registrations] == [
        ('json-hook', False)
    ]
    assert [finding.code for finding in findings] == ['codex_hook_trust_unknown']


def test_stop_wrapper_reports_only_unservable_native_event(tmp_path: Path) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(
        source,
        {
            'Stop': [
                {
                    'hooks': [
                        {
                            'type': 'command',
                            'command': 'sequential-hooks --agent codex child',
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
                load_host('codex'),
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


def test_system_registration_is_managed_policy_without_user_trust_review(
    tmp_path: Path,
) -> None:
    system = tmp_path / 'system/etc/codex/hooks.json'
    _write_json(
        system,
        {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'system-hook'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path / 'home', tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['system-hook']
    assert [finding.code for finding in findings] == ['codex_system_policy_source']
    assert findings[0].correction is None
    assert 'managed policy source' in findings[0].message
    assert '/hooks inside Codex' not in findings[0].message


def test_toml_hook_state_is_not_treated_as_event(tmp_path: Path) -> None:
    source = tmp_path / '.codex/config.toml'
    source.parent.mkdir(parents=True)
    source.write_text(
        """
[hooks.state]

[hooks.state."user-hook"]
trusted_hash = "sha256:example"
enabled = true

[[hooks.PreToolUse]]
[[hooks.PreToolUse.hooks]]
type = "command"
command = "safe"
"""
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe']
    assert [finding.code for finding in findings] == ['codex_hook_trust_unknown']


@pytest.mark.parametrize('key', ['command_windows', 'commandWindows'])
def test_toml_windows_command_aliases_are_normalized(
    tmp_path: Path,
    key: str,
) -> None:
    source = tmp_path / '.codex/config.toml'
    source.parent.mkdir(parents=True)
    source.write_text(
        f"""
[[hooks.PreToolUse]]
[[hooks.PreToolUse.hooks]]
type = "command"
command = "posix-command"
{key} = "windows-command"
"""
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].command_windows == 'windows-command'
    assert registrations[0].windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL
    assert [finding.code for finding in findings] == ['codex_hook_trust_unknown']


def test_unsupported_and_async_handlers_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / '.codex/hooks.json'
    _write_json(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {'type': 'prompt', 'command': 'private-prompt'},
                        {'type': 'agent', 'command': 'private-agent'},
                        {
                            'type': 'command',
                            'command': 'private-async',
                            'async': True,
                        },
                        {
                            'type': 'command',
                            'command': 'supported',
                            'async': False,
                        },
                    ]
                }
            ]
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['supported']
    assert [finding.code for finding in findings] == [
        'codex_hook_type_unsupported',
        'codex_hook_type_unsupported',
        'codex_async_unsupported',
        'codex_hook_trust_unknown',
    ]
    rendered = ' '.join(finding.message for finding in findings)
    assert 'private-prompt' not in rendered
    assert 'private-agent' not in rendered
    assert 'private-async' not in rendered


@pytest.mark.parametrize('windows_override', [None, 'windows-command'])
def test_windows_shell_form_is_explicitly_uninspectable(
    tmp_path: Path,
    windows_override: str | None,
) -> None:
    source = tmp_path / '.codex/hooks.json'
    hook: dict[str, object] = {
        'type': 'command',
        'command': 'portable-command',
    }
    if windows_override is not None:
        hook['commandWindows'] = windows_override
    _write_json(source, {'PreToolUse': [{'hooks': [hook]}]})

    registrations, _findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    inspected = inspect_command(registrations[0], windows=True)

    assert registrations[0].windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL
    assert inspected.argv is None
    assert inspected.error == 'windows_shell launch semantics cannot be inspected statically'
