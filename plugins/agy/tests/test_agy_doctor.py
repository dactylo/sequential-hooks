# pyright: reportPrivateUsage=false

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from plugins.agy._sequential_hooks import _doctor as _agy
from plugins.agy._sequential_hooks._doctor import (
    _discover_sources,
    _inspection_limitations,
    _load_registrations,
)
from sequential_hooks._doctor._command import inspect_command
from sequential_hooks._doctor._common import analyze_registration
from sequential_hooks._registry import load_host
from sequential_hooks.hosts import (
    Finding,
    FindingSeverity,
    LaunchSemantics,
    SourceKind,
)

_MALFORMED_HOOKS_NAME_CASES = cast(
    'tuple[tuple[object, str], ...]',
    (
        ([], 'agy_named_hook_malformed'),
        ({'PreToolUse': {}}, 'agy_event_malformed'),
        ({'PreToolUse': [None]}, 'agy_matcher_group_malformed'),
        ({'PreToolUse': [{'matcher': None, 'hooks': []}]}, 'agy_matcher_malformed'),
        ({'PreToolUse': [{}]}, 'agy_handler_list_malformed'),
    ),
)


def _non_global_codes(findings: object) -> list[str]:
    typed = cast('tuple[Finding, ...]', findings)
    return [finding.code for finding in typed]


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _write_hook_named_hooks(path: Path, hooks: object, **settings: object) -> None:
    _write_json(path, {'hooks': hooks, **settings})


def _write_named(path: Path, hooks: dict[str, object]) -> None:
    _write_json(path, hooks)


@pytest.mark.parametrize(
    ('source_bytes', 'expected_code'),
    [
        (b'\xffprivate-byte-sentinel', 'agy_source_encoding_malformed'),
        (b'{"private": NaN}', 'agy_json_malformed'),
        (b'{"private": Infinity}', 'agy_json_malformed'),
        (b'{"private": -Infinity}', 'agy_json_malformed'),
        (f'{{"private": {"9" * 4301}}}'.encode(), 'agy_json_malformed'),
        (b'[]', 'agy_settings_malformed'),
        ((b'{"private":' * 1_500) + b'{}' + (b'}' * 1_500), 'agy_json_malformed'),
        (
            (
                '{"private":{"PreToolUse":[{"hooks":[{"command":"private-command",'
                f'"timeout":{"9" * 401}'
                '}] }]}}'
            ).encode(),
            'agy_timeout_malformed',
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
    user = tmp_path / '.gemini/config/hooks.json'
    project = tmp_path / 'project/.agents/hooks.json'
    user.parent.mkdir(parents=True)
    user.write_bytes(source_bytes)
    _write_hook_named_hooks(
        project,
        {'PostToolUse': [{'hooks': [{'command': 'safe-command', 'timeout': 30}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe-command']
    assert expected_code in _non_global_codes(findings)
    rendered = ' '.join(finding.message for finding in findings)
    assert 'private-byte-sentinel' not in rendered
    assert 'private-command' not in rendered


@pytest.mark.parametrize(
    ('outer_timeout', 'expected'),
    [
        (10, True),
        (11, False),
    ],
)
def test_chain_budget_accounts_for_every_step_and_overhead(
    tmp_path: Path,
    outer_timeout: int,
    expected: bool,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'command': (
                                'sequential-hooks --agent agy --event PreToolUse '
                                '--step-timeout 5 first -- second'
                            ),
                            'timeout': outer_timeout,
                        }
                    ]
                }
            ]
        },
    )

    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = _agy._analyze_registration(registration, inspected, windows=False)

    budget_findings = [
        finding for finding in findings if finding.code == 'agy_chain_budget_uncontained'
    ]
    assert bool(budget_findings) is expected
    if expected:
        assert budget_findings[0].severity is FindingSeverity.WARNING
        assert budget_findings[0].correction == (
            'Set the outer Antigravity handler timeout above the configured chain budget.'
        )


def test_command_string_uses_verified_shell_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'command': ('sequential-hooks --agent agy --event PreToolUse child'),
                            'timeout': 30,
                        }
                    ]
                }
            ]
        },
    )

    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = _agy._analyze_registration(registration, inspected, windows=False)

    assert len(registrations) == 1
    assert registrations[0].launch_semantics is LaunchSemantics.POSIX_SHELL
    assert registrations[0].windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL
    assert registrations[0].args is None
    assert 'agy_wrapper_contract_uninspectable' not in _non_global_codes(findings)
    assert 'event_missing' not in _non_global_codes(findings)


def test_default_timeout_is_dated_information(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {'PreToolUse': [{'hooks': [{'command': 'child'}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations[0].timeout == 30.0
    assert findings == ()
    timeout = registrations[0].configuration_findings
    assert timeout == (
        Finding(
            FindingSeverity.INFO,
            'agy_timeout_default_documented',
            'As documented on 2026-07-28, Antigravity defaults command handler '
            'timeouts to 30 seconds',
        ),
    )
    assert len(timeout) == 1
    assert timeout[0].severity is FindingSeverity.INFO
    assert '2026-07-28' in timeout[0].message
    assert '30 seconds' in timeout[0].message


def test_discover_sources_in_documented_order(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    root = tmp_path / 'repo'
    cwd = root / 'nested'
    cwd.mkdir(parents=True)
    (root / '.git').mkdir()
    expected = [
        (home / '.gemini/config/hooks.json', SourceKind.USER),
        (root / '.agents/hooks.json', SourceKind.PROJECT),
        (cwd / '.agents/hooks.json', SourceKind.PROJECT),
    ]
    for path, _ in expected:
        _write_json(path, {})

    sources = _discover_sources(home, cwd, tmp_path / 'system')

    assert [(source.path, source.kind, source.exists) for source in sources] == [
        (path, kind, True) for path, kind in expected
    ]


def test_documented_named_shape_preserves_registration_fields(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_named(
        source,
        {
            'disabled-wrapper': {
                'enabled': False,
                'PreToolUse': [
                    {
                        'matcher': '^run_command$',
                        'hooks': [
                            {
                                'type': 'command',
                                'command': (
                                    'sequential-hooks --agent agy --event PreToolUse first'
                                ),
                                'timeout': 45.5,
                            }
                        ],
                    }
                ],
            },
            'defaulted-wrapper': {
                'PostToolUse': [
                    {
                        'hooks': [
                            {
                                'command': (
                                    'sequential-hooks --agent agy --event PostToolUse second'
                                ),
                            }
                        ]
                    }
                ]
            },
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

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
            source,
            'PreToolUse',
            '^run_command$',
            'sequential-hooks --agent agy --event PreToolUse first',
            None,
            LaunchSemantics.POSIX_SHELL,
            None,
            LaunchSemantics.WINDOWS_SHELL,
            45.5,
            False,
            'command',
        ),
        (
            source,
            'PostToolUse',
            None,
            'sequential-hooks --agent agy --event PostToolUse second',
            None,
            LaunchSemantics.POSIX_SHELL,
            None,
            LaunchSemantics.WINDOWS_SHELL,
            30.0,
            True,
            'command',
        ),
    ]
    assert findings == ()
    assert [
        [finding.code for finding in registration.configuration_findings]
        for registration in registrations
    ] == [[], ['agy_timeout_default_documented']]


@pytest.mark.parametrize(
    'command',
    [
        ('sequential-hooks --agent codex --agent agy --event PreToolUse child'),
        ('sequential-hooks --agent codex --event PreToolUse --event PostToolUse child'),
    ],
)
def test_duplicate_literal_options_remain_uninspectable(
    tmp_path: Path,
    command: str,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'command': command,
                            'timeout': 30,
                        }
                    ]
                }
            ]
        },
    )

    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = _agy._analyze_registration(registration, inspected, windows=False)

    codes = _non_global_codes(findings)
    assert 'agy_wrapper_contract_uninspectable' in codes
    assert findings[0].correction is not None
    assert 'wrapper' in findings[0].correction
    assert 'host_missing' not in codes
    assert 'wrong_host' not in codes


def test_dynamic_command_has_no_malformed_wrapper_warning(tmp_path: Path) -> None:
    _write_hook_named_hooks(
        tmp_path / '.gemini/config/hooks.json',
        {'PreToolUse': [{'hooks': [{'command': '~/bin/hook', 'timeout': 30}]}]},
    )
    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = (
        *analyze_registration(
            load_host('agy'),
            (registration, inspected),
            path_value='',
            path_ext=None,
            windows=False,
        ),
        *_agy._analyze_registration(registration, inspected, windows=False),
    )
    assert [finding.code for finding in findings] == ['command_dynamic']


def test_hook_named_hooks_uses_named_schema(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'matcher': '^write_to_file$',
                    'hooks': [
                        {
                            'command': ('sequential-hooks --agent agy --event PreToolUse child'),
                            'timeout': 40,
                        }
                    ],
                }
            ]
        },
    )

    registrations, _ = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert len(registrations) == 1
    registration = registrations[0]
    assert registration.source.path == source
    assert registration.event == 'PreToolUse'
    assert registration.matcher == '^write_to_file$'
    assert registration.args is None
    assert registration.launch_semantics is LaunchSemantics.POSIX_SHELL
    assert registration.windows_launch_semantics is LaunchSemantics.WINDOWS_SHELL
    assert registration.timeout == 40.0
    assert registration.enabled is True
    assert registration.type == 'command'


def test_invalid_named_hook_does_not_fall_back_to_event_map(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'enabled': [],
            'PreToolUse': [{'hooks': [{'command': 'live-only-command', 'timeout': 30}]}],
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert registrations == ()
    codes = [finding.code for finding in findings]
    assert 'agy_enabled_malformed' in codes
    assert all('live-only-command' not in finding.message for finding in findings)


def test_limitations_cover_uninspectable_sources_without_plugin_scanning() -> None:
    limitations = _inspection_limitations()

    assert any('organization-managed' in limitation for limitation in limitations)
    assert any('product-internal' in limitation for limitation in limitations)
    assert any('plugin' in limitation for limitation in limitations)
    assert any('exact readable source paths' in limitation for limitation in limitations)


def test_loading_is_process_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    marker = tmp_path / 'marker'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
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
    ('handler', 'expected_code'),
    [
        ({}, 'agy_command_malformed'),
        ({'command': ''}, 'agy_command_malformed'),
        ({'command': 'private-sentinel', 'args': None}, 'agy_args_malformed'),
        ({'command': 'private-sentinel', 'args': [1]}, 'agy_args_malformed'),
        ({'command': 'private-sentinel', 'timeout': True}, 'agy_timeout_malformed'),
        ({'command': 'private-sentinel', 'timeout': 0}, 'agy_timeout_malformed'),
        ({'command': 'private-sentinel', 'timeout': 10**400}, 'agy_timeout_malformed'),
        (
            {'type': 'prompt', 'command': 'private-sentinel'},
            'agy_hook_type_unsupported',
        ),
    ],
)
def test_malformed_handler_fields_are_isolated(
    tmp_path: Path,
    handler: dict[str, object],
    expected_code: str,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(source, {'PreToolUse': [{'hooks': [handler]}]})

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert expected_code in _non_global_codes(findings)
    assert all('private-sentinel' not in finding.message for finding in findings)


@pytest.mark.parametrize(
    ('hooks', 'expected_code'),
    _MALFORMED_HOOKS_NAME_CASES,
)
def test_malformed_hook_named_hooks_is_reported_without_values(
    tmp_path: Path,
    hooks: object,
    expected_code: str,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(source, hooks, unrelated='private-sentinel')

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert expected_code in _non_global_codes(findings)
    assert all('private-sentinel' not in finding.message for finding in findings)


@pytest.mark.parametrize(
    ('named_hook', 'expected_code'),
    [
        ([], 'agy_named_hook_malformed'),
        ({'enabled': None}, 'agy_enabled_malformed'),
        ({'PreToolUse': {}}, 'agy_event_malformed'),
    ],
)
def test_malformed_named_shape_is_reported(
    tmp_path: Path,
    named_hook: object,
    expected_code: str,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_named(source, {'private-name': named_hook})

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert not registrations
    assert expected_code in _non_global_codes(findings)
    assert all('private-name' not in finding.message for finding in findings)


def test_malformed_sources_are_isolated_and_values_are_not_rendered(tmp_path: Path) -> None:
    user = tmp_path / '.gemini/config/hooks.json'
    project = tmp_path / 'project/.agents/hooks.json'
    user.parent.mkdir(parents=True)
    user.write_text('{"hooks": {"PreToolUse": []}, "hooks": {"private-sentinel": "secret"}}')
    _write_hook_named_hooks(
        project,
        {'PostToolUse': [{'hooks': [{'command': 'safe-command', 'timeout': 30}]}]},
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.command for registration in registrations] == ['safe-command']
    assert 'agy_json_malformed' in _non_global_codes(findings)
    assert all('private-sentinel' not in finding.message for finding in findings)
    assert all('secret' not in finding.message for finding in findings)


def test_missing_sources_are_retained(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.git').mkdir()
    sources = _discover_sources(
        tmp_path / 'home',
        project,
        tmp_path / 'system',
    )

    assert sources
    assert all(source.exists is False for source in sources)
    assert all(source.kind is not SourceKind.SYSTEM for source in sources)


def test_named_hook_called_hooks_preserves_state_and_siblings(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_named(
        source,
        {
            'first': {'PreToolUse': [{'hooks': [{'command': 'first-command', 'timeout': 30}]}]},
            'hooks': {
                'enabled': False,
                'PreToolUse': [{'hooks': [{'command': 'literal-hooks-name', 'timeout': 30}]}],
            },
            'last': {'PostToolUse': [{'hooks': [{'command': 'last-command', 'timeout': 30}]}]},
        },
    )

    registrations, _ = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(registration.command, registration.enabled) for registration in registrations] == [
        ('first-command', True),
        ('literal-hooks-name', False),
        ('last-command', True),
    ]


def test_named_hooks_preserve_mixed_source_order(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_named(
        source,
        {
            'first': {'PreToolUse': [{'hooks': [{'command': 'first-command', 'timeout': 30}]}]},
            'hooks': {
                'PreToolUse': [{'hooks': [{'command': 'ambiguous-command', 'timeout': 30}]}]
            },
            'last': {'PostToolUse': [{'hooks': [{'command': 'last-command', 'timeout': 30}]}]},
        },
    )

    registrations, _ = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [(registration.command, registration.enabled) for registration in registrations] == [
        ('first-command', True),
        ('ambiguous-command', True),
        ('last-command', True),
    ]


@pytest.mark.parametrize(
    ('command', 'event', 'expected_code'),
    [
        ('sequential-hooks --agent agy child', 'PreToolUse', 'event_missing'),
        (
            'sequential-hooks --agent agy --event PostToolUse child',
            'PreToolUse',
            'wrong_event',
        ),
        (
            'sequential-hooks --agent agy --event PreToolUse child',
            'PreToolUse',
            None,
        ),
    ],
)
def test_shell_command_checks_registered_wrapper_event(
    tmp_path: Path,
    command: str,
    event: str,
    expected_code: str | None,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {event: [{'hooks': [{'command': command, 'timeout': 30}]}]},
    )

    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = analyze_registration(
        load_host('agy'),
        (registration, inspected),
        path_value=None,
        path_ext=None,
        windows=False,
    )
    assert _agy._analyze_registration(registration, inspected, windows=False) == ()

    codes = _non_global_codes(findings)
    if expected_code is None:
        assert 'event_missing' not in codes
        assert 'wrong_event' not in codes
    else:
        assert expected_code in codes


@pytest.mark.parametrize(
    ('command', 'expected_code'),
    [
        ('sequential-hooks child', 'host_missing'),
        ('sequential-hooks --agent codex child', 'wrong_host'),
        (
            'sequential-hooks --agent codex --event PreToolUse child',
            'wrong_host',
        ),
    ],
)
def test_shell_command_preserves_safe_host_diagnoses(
    tmp_path: Path,
    command: str,
    expected_code: str,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'PreToolUse': [
                {
                    'hooks': [
                        {
                            'command': command,
                            'timeout': 30,
                        }
                    ]
                }
            ]
        },
    )

    registrations, source_findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )
    assert source_findings == ()
    registration = registrations[0]
    inspected = inspect_command(registration, windows=False)
    findings = analyze_registration(
        load_host('agy'),
        (registration, inspected),
        path_value=None,
        path_ext=None,
        windows=False,
    )
    assert _agy._analyze_registration(registration, inspected, windows=False) == ()

    matching = [finding for finding in findings if finding.code == expected_code]
    assert len(matching) == 1
    assert matching[0].correction == 'Use --agent agy.'


def test_stop_wrappers_report_only_unservable_native_event(tmp_path: Path) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    _write_hook_named_hooks(
        source,
        {
            'Stop': [
                {
                    'hooks': [
                        {
                            'command': ('sequential-hooks --agent agy --event PostToolUse child'),
                            'timeout': 30,
                        },
                        {'command': 'third-party', 'timeout': 30},
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
                load_host('agy'),
                (registration, inspect_command(registration, windows=False)),
                path_value=None,
                path_ext=None,
                windows=False,
            )
        ),
    )

    matching = [finding for finding in findings if finding.code == 'native_event_unsupported']
    assert len(matching) == 1
    assert all(finding.severity is FindingSeverity.ERROR for finding in matching)
    assert 'agy_native_event_unknown' not in _non_global_codes(findings)
    assert {'event_missing', 'wrong_event', 'event_forbidden'}.isdisjoint(
        _non_global_codes(findings)
    )


def test_unknown_native_event_is_preserved_without_entering_shared_corrections(
    tmp_path: Path,
) -> None:
    source = tmp_path / '.gemini/config/hooks.json'
    event = 'Sideways'
    _write_hook_named_hooks(
        source,
        {
            event: [
                {
                    'hooks': [
                        {
                            'command': 'sequential-hooks --agent agy child',
                            'timeout': 30,
                        }
                    ]
                }
            ]
        },
    )

    registrations, findings = _load_registrations(
        _discover_sources(tmp_path, tmp_path / 'project', tmp_path / 'system')
    )

    assert [registration.event for registration in registrations] == [event]
    assert findings == ()
    native_event = registrations[0].configuration_findings
    assert native_event == (
        Finding(
            FindingSeverity.WARNING,
            'agy_native_event_unknown',
            'Antigravity native event is not a documented Antigravity hook event',
            'Use a documented Antigravity hook event name.',
        ),
    )
    registration = registrations[0]
    assert (
        _agy._analyze_registration(
            registration,
            inspect_command(registration, windows=False),
            windows=False,
        )
        == ()
    )
    assert len(native_event) == 1
    assert native_event[0].message.endswith(
        'Antigravity native event is not a documented Antigravity hook event'
    )
    assert native_event[0].correction == 'Use a documented Antigravity hook event name.'
    assert 'event_missing' not in _non_global_codes(findings)
    assert 'wrong_event' not in _non_global_codes(findings)
    assert all(
        event not in finding.message and event not in (finding.correction or '')
        for finding in native_event
    )
    assert len(native_event[0].message) <= 120
    assert len(native_event[0].correction or '') <= 120
