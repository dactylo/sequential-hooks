from pathlib import Path

import pytest

from sequential_hooks._doctor import _common
from sequential_hooks._doctor._command import inspect_command
from sequential_hooks._doctor._common import (
    analyze_registration,
    analyze_registration_pairs,
    inspect_platform,
)
from sequential_hooks._executable import ExecutableResolution, ExecutableStatus
from sequential_hooks._registry import load_host
from sequential_hooks.hosts import FindingSeverity, LaunchSemantics
from tests.doctor._helpers import registration as _registration


@pytest.mark.usefixtures('distribution_root')
def test_analysis_preserves_registration_and_pair_finding_order(
    tmp_path: Path,
) -> None:
    registrations = (
        _registration(
            'sequential-hooks --agent claude missing-child',
            matcher='^Bash$',
            timeout=1.0,
        ),
        _registration(
            'sequential-hooks --agent claude missing-child',
            matcher='^Bash$',
            timeout=None,
            enabled=None,
        ),
        _registration(
            'sequential-hooks --agent codex missing-child',
            matcher='^Read$',
        ),
    )
    inspected_registrations = tuple(
        (registration, inspect_command(registration, windows=False))
        for registration in registrations
    )
    findings = (
        *(
            finding
            for pair in inspected_registrations
            for finding in analyze_registration(
                load_host('codex'),
                pair,
                path_value=str(tmp_path),
                path_ext=None,
                windows=False,
            )
        ),
        *analyze_registration_pairs(inspected_registrations, windows=False),
    )

    assert [finding.code for finding in findings] == [
        'outer_timeout_too_small',
        'wrapper_executable_missing',
        'wrong_host',
        'child_executable_missing',
        'outer_timeout_only',
        'enabled_unknown',
        'outer_timeout_missing',
        'wrapper_executable_missing',
        'wrong_host',
        'child_executable_missing',
        'outer_timeout_only',
        'wrapper_executable_missing',
        'child_executable_missing',
        'outer_timeout_only',
        'duplicate_wrapper',
        'definite_matcher_overlap',
        'possible_matcher_overlap',
        'possible_matcher_overlap',
    ]


@pytest.mark.usefixtures('distribution_root')
def test_analysis_reports_event_and_uninspectable_states() -> None:
    registrations = (
        _registration(
            'sequential-hooks',
            args=('--agent', 'agy', '--event', 'PreToolUse', 'child'),
            semantics=LaunchSemantics.DIRECT_ARGV,
            event='PostToolUse',
        ),
        _registration(semantics=LaunchSemantics.UNKNOWN, enabled=False),
    )

    inspected_registrations = tuple(
        (registration, inspect_command(registration, windows=False))
        for registration in registrations
    )
    findings = (
        *(
            finding
            for pair in inspected_registrations
            for finding in analyze_registration(
                load_host('agy'),
                pair,
                path_value=None,
                path_ext=None,
                windows=False,
            )
        ),
        *analyze_registration_pairs(inspected_registrations, windows=False),
    )
    codes = [finding.code for finding in findings]

    assert 'wrong_event' in codes
    assert 'command_uninspectable' in codes
    assert 'registration_disabled' in codes


@pytest.mark.usefixtures('distribution_root')
def test_analysis_reports_missing_executables_and_literal_child_duplicate(
    tmp_path: Path,
) -> None:
    registrations = (
        _registration('sequential-hooks --agent codex missing-child'),
        _registration('missing-child', semantics=LaunchSemantics.POSIX_SHELL),
    )

    inspected_registrations = tuple(
        (registration, inspect_command(registration, windows=False))
        for registration in registrations
    )
    findings = (
        *(
            finding
            for pair in inspected_registrations
            for finding in analyze_registration(
                load_host('codex'),
                pair,
                path_value=str(tmp_path),
                path_ext=None,
                windows=False,
            )
        ),
        *analyze_registration_pairs(inspected_registrations, windows=False),
    )
    codes = [finding.code for finding in findings]

    assert 'wrapper_executable_missing' in codes
    assert 'child_executable_missing' in codes
    assert 'duplicate_child_registration' in codes


@pytest.mark.usefixtures('distribution_root')
def test_analysis_reports_unknown_activation_for_an_unrecognized_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_host('codex')
    monkeypatch.setattr(spec, 'name', 'third-party')
    registration = _registration('literal-child', enabled=None)

    findings = analyze_registration(
        spec,
        (registration, inspect_command(registration, windows=False)),
        path_value=str(tmp_path),
        path_ext=None,
        windows=False,
    )

    unknowns = [finding for finding in findings if finding.code == 'enabled_unknown']
    assert len(unknowns) == 1
    assert unknowns[0].severity is FindingSeverity.WARNING
    assert unknowns[0].correction == (
        'Reconcile hook activation settings in the discovered sources.'
    )


@pytest.mark.usefixtures('distribution_root')
def test_analysis_reports_wrapper_contract_and_duplicates(tmp_path: Path) -> None:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    wrapper = bin_dir / 'sequential-hooks'
    child = bin_dir / 'child'
    for executable in (wrapper, child):
        executable.write_text('')
        executable.chmod(0o755)
    first = _registration(
        'sequential-hooks --agent claude child',
        timeout=1.0,
    )
    second = _registration(
        'sequential-hooks --agent claude child',
        matcher='*',
        timeout=None,
        enabled=None,
    )

    inspected_registrations = tuple(
        (registration, inspect_command(registration, windows=False))
        for registration in (first, second)
    )
    findings = (
        *(
            finding
            for pair in inspected_registrations
            for finding in analyze_registration(
                load_host('codex'),
                pair,
                path_value=str(bin_dir),
                path_ext=None,
                windows=False,
            )
        ),
        *analyze_registration_pairs(inspected_registrations, windows=False),
    )
    codes = [finding.code for finding in findings]

    assert 'wrong_host' in codes
    assert 'outer_timeout_too_small' in codes
    assert 'outer_timeout_missing' in codes
    assert 'enabled_unknown' in codes
    assert 'duplicate_wrapper' not in codes
    assert 'definite_matcher_overlap' in codes
    assert 'outer_timeout_only' in codes


@pytest.mark.parametrize(
    ('windows', 'literal', 'expected'),
    [
        (True, 'CHILD arg', True),
        (True, 'CHILD ARG', False),
        (True, 'CHILD different', False),
        (False, 'Child ARG', False),
        (False, 'child arg', True),
    ],
)
def test_duplicate_child_comparison_uses_platform_rules(
    windows: bool,
    literal: str,
    expected: bool,
) -> None:
    if windows:
        wrapper = _registration(
            command_windows='sequential-hooks.exe',
            windows_semantics=LaunchSemantics.DIRECT_ARGV,
            args=('--agent', 'codex', 'child', 'arg'),
        )
        child = _registration(
            command_windows=literal.split(maxsplit=1)[0],
            windows_semantics=LaunchSemantics.DIRECT_ARGV,
            args=tuple(literal.split()[1:]),
        )
    else:
        wrapper = _registration('sequential-hooks --agent codex child arg')
        child = _registration(literal)

    findings = analyze_registration_pairs(
        (
            (registration, inspect_command(registration, windows=windows))
            for registration in (wrapper, child)
        ),
        windows=windows,
    )

    assert ('duplicate_child_registration' in {finding.code for finding in findings}) is expected


def test_duplicate_wrapper_requires_repeated_event_and_matcher() -> None:
    findings = analyze_registration_pairs(
        (
            (registration, inspect_command(registration, windows=False))
            for registration in (
                _registration(matcher='same'),
                _registration(matcher='same'),
            )
        ),
        windows=False,
    )

    codes = [finding.code for finding in findings]

    assert codes.count('duplicate_wrapper') == 1
    assert codes.count('definite_matcher_overlap') == 1


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    'command',
    [
        'sequential-hooks --agent codex --event PreToolUse child',
        'sequential-hooks --agent agy --event PreToolUse child',
    ],
)
def test_event_forbidden_correction_repairs_wrong_host_in_one_edit(
    command: str,
) -> None:
    registration = _registration(
        command,
        event='PreToolUse',
    )
    findings = analyze_registration(
        load_host('claude'),
        (registration, inspect_command(registration, windows=False)),
        path_value=None,
        path_ext=None,
        windows=False,
    )

    matching = [finding for finding in findings if finding.code == 'event_forbidden']
    assert len(matching) == 1
    assert matching[0].correction == ('Use --agent claude and remove the --event option.')


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    'command',
    [
        'sequential-hooks --bogus --agent codex child',
        'sequential-hooks --step-timeout --agent codex child',
        'sequential-hooks --agent codex --bogus child',
        'sequential-hooks --agent codex --agent codex child',
        ('sequential-hooks --agent agy --event PreToolUse --event PostToolUse child'),
        'sequential-hooks --agent --event PostToolUse child',
        'sequential-hooks --agent agy --event --allow-on-failure child',
        'sequential-hooks --allow-on-failure --allow-on-failure child',
        'sequential-hooks --step-timeout invalid child',
        'sequential-hooks --agent codex',
        "sequential-hooks --agent '' child",
        "sequential-hooks --agent agy --event '' child",
    ],
)
def test_malformed_wrapper_prefix_remains_uninspectable(command: str) -> None:
    registration = _registration(command)
    findings = analyze_registration(
        load_host('codex'),
        (registration, inspect_command(registration, windows=False)),
        path_value=None,
        path_ext=None,
        windows=False,
    )

    codes = [finding.code for finding in findings]

    assert 'command_uninspectable' in codes
    finding = next(item for item in findings if item.code == 'command_uninspectable')
    assert 'wrapper' in (finding.correction or '')
    assert 'grammar' in (finding.correction or '')
    assert not {
        'event_forbidden',
        'event_missing',
        'host_missing',
        'wrong_event',
        'wrong_host',
    }.intersection(codes)


@pytest.mark.usefixtures('distribution_root')
def test_partial_wrapper_context_does_not_read_child_options() -> None:
    registration = _registration('sequential-hooks child --agent unknown')
    findings = analyze_registration(
        load_host('codex'),
        (registration, inspect_command(registration, windows=False)),
        path_value=None,
        path_ext=None,
        windows=False,
    )

    codes = [finding.code for finding in findings]
    host_missing = next(finding for finding in findings if finding.code == 'host_missing')

    assert 'host_missing' in codes
    assert 'wrong_host' not in codes
    assert host_missing.message == 'wrapper agent is missing'


@pytest.mark.usefixtures('distribution_root')
@pytest.mark.parametrize(
    ('host', 'command', 'event', 'code', 'correction'),
    [
        (
            'codex',
            'sequential-hooks child',
            'PostToolUse',
            'host_missing',
            'Use --agent codex.',
        ),
        (
            'codex',
            'sequential-hooks --allow-on-failure child',
            'PostToolUse',
            'host_missing',
            'Use --agent codex.',
        ),
        (
            'codex',
            'sequential-hooks --step-timeout 1 child',
            'PostToolUse',
            'host_missing',
            'Use --agent codex.',
        ),
        (
            'codex',
            'sequential-hooks --agent unknown child',
            'PostToolUse',
            'wrong_host',
            'Use --agent codex.',
        ),
        (
            'agy',
            'sequential-hooks --agent agy child',
            'PreToolUse',
            'event_missing',
            'Use --event PreToolUse.',
        ),
        (
            'agy',
            'sequential-hooks --agent agy --event Other child',
            'PostToolUse',
            'wrong_event',
            'Use --event PostToolUse.',
        ),
        (
            'codex',
            'sequential-hooks --agent codex --event PostToolUse child',
            'PostToolUse',
            'event_forbidden',
            'Use --agent codex and remove the --event option.',
        ),
    ],
)
def test_partial_wrapper_contract_has_stable_finding(
    host: str,
    command: str,
    event: str,
    code: str,
    correction: str,
) -> None:
    registration = _registration(command, event=event)
    findings = analyze_registration(
        load_host(host),
        (registration, inspect_command(registration, windows=False)),
        path_value=None,
        path_ext=None,
        windows=False,
    )

    matching = [finding for finding in findings if finding.code == code]

    assert len(matching) == 1
    assert matching[0].correction == correction
    assert all(finding.code != 'command_uninspectable' for finding in findings)


def test_platform_report_is_static(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.python_implementation', lambda: 'CPython')
    monkeypatch.setattr('sys.platform', 'linux')

    report = inspect_platform()

    assert report.interpreter == 'CPython'
    assert report.containment == 'posix'
    assert report.job_object_configurable is None


def test_posix_shell_comment_text_does_not_hide_child_duplicate() -> None:
    findings = analyze_registration_pairs(
        (
            (registration, inspect_command(registration, windows=False))
            for registration in (
                _registration('sequential-hooks --agent codex child # ignored'),
                _registration('child'),
            )
        ),
        windows=False,
    )

    assert 'duplicate_child_registration' in {finding.code for finding in findings}


def test_regex_overlap_is_not_executed_or_claimed_definite() -> None:
    findings = analyze_registration_pairs(
        (
            (registration, inspect_command(registration, windows=False))
            for registration in (
                _registration(matcher='foo.*'),
                _registration(matcher='foo.+'),
            )
        ),
        windows=False,
    )

    overlap = [finding for finding in findings if finding.code == 'possible_matcher_overlap']

    assert len(overlap) == 1
    assert overlap[0].severity is FindingSeverity.INFO


@pytest.mark.usefixtures('distribution_root')
def test_windows_batch_shim_finding_is_bounded_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = 'secret-path-sentinel'
    deep = tmp_path / sentinel / ('x' * 300)
    shim = deep / 'sequential-hooks.cmd'
    original_resolve = _common.resolve_executable
    calls: list[tuple[str, str | None, bool, str | None]] = []

    def resolve_long_shim(
        executable: str,
        *,
        path_value: str | None,
        windows: bool,
        pathext_value: str | None,
    ) -> ExecutableResolution:
        calls.append((executable, path_value, windows, pathext_value))
        if executable == 'sequential-hooks':
            return ExecutableResolution(executable, shim, ExecutableStatus.UNSUPPORTED_SHIM)
        return original_resolve(
            executable,
            path_value=path_value,
            windows=windows,
            pathext_value=pathext_value,
        )

    monkeypatch.setattr(_common, 'resolve_executable', resolve_long_shim)
    registration = _registration(
        command_windows='sequential-hooks',
        windows_semantics=LaunchSemantics.DIRECT_ARGV,
        args=('--agent', 'codex', 'child'),
    )

    findings = analyze_registration(
        load_host('codex'),
        (registration, inspect_command(registration, windows=True)),
        path_value=str(deep),
        path_ext='.CMD',
        windows=True,
    )
    unsupported = [
        finding for finding in findings if finding.code == 'wrapper_executable_unsupported'
    ]

    assert len(str(shim)) > 260
    assert calls[0] == ('sequential-hooks', str(deep), True, '.CMD')
    assert len(unsupported) == 1
    assert unsupported[0].message == (
        'wrapper executable resolves to an unsupported Windows shim at '
        '[redacted]/sequential-hooks.cmd'
    )
    assert unsupported[0].correction == (
        'Replace the Windows batch shim with a real .exe or .com executable entry point.'
    )
    assert sentinel not in unsupported[0].message
    assert sentinel not in (unsupported[0].correction or '')
    assert len(unsupported[0].message) <= 120
    assert len(unsupported[0].correction or '') <= 120


@pytest.mark.usefixtures('distribution_root')
def test_windows_child_shim_redacts_requested_name_and_directory(tmp_path: Path) -> None:
    directory_sentinel = 'private-directory-sentinel'
    input_sentinel = f'private-input-sentinel-{"x" * 100}'
    shim_dir = tmp_path / directory_sentinel
    shim_dir.mkdir()
    (shim_dir / f'{input_sentinel}.cmd').write_text('')
    registration = _registration(
        command_windows=input_sentinel,
        windows_semantics=LaunchSemantics.DIRECT_ARGV,
        args=(),
    )

    findings = analyze_registration(
        load_host('codex'),
        (registration, inspect_command(registration, windows=True)),
        path_value=str(shim_dir),
        path_ext='.CMD',
        windows=True,
    )
    unsupported = [
        finding for finding in findings if finding.code == 'child_executable_unsupported'
    ]

    assert len(unsupported) == 1
    assert unsupported[0].message == (
        'child executable resolves to an unsupported Windows shim at [redacted]/[shim].cmd'
    )
    assert unsupported[0].correction == (
        'Replace the Windows batch shim with a real .exe or .com executable entry point.'
    )
    assert directory_sentinel not in unsupported[0].message
    assert input_sentinel not in unsupported[0].message
    assert len(unsupported[0].message) <= 120
    assert len(unsupported[0].correction or '') <= 120
