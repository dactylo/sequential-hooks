import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import decode_text, process_transcript, run_setup

_ROOT = Path(__file__).parents[1]
_SCRIPT = _ROOT / 'tests' / '_assert_ci_tier.py'
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'ci.yml'


def _active_lines(workflow: str) -> str:
    return '\n'.join(
        line.split(' #', maxsplit=1)[0]
        for raw_line in workflow.splitlines()
        if (line := raw_line.rstrip()) and not line.lstrip().startswith('#')
    )


def _assert_bounded_stderr(result: subprocess.CompletedProcess[bytes]) -> None:
    assert decode_text(result.stdout) == '', process_transcript(result)
    assert decode_text(result.stderr).endswith('\n'), process_transcript(result)
    assert decode_text(result.stderr).count('\n') == 1, process_transcript(result)
    assert len(decode_text(result.stderr)) <= 200, process_transcript(result)


def _job_blocks(workflow: str) -> dict[str, str]:
    workflow = workflow.split('\njobs:\n', maxsplit=1)[1]
    matches = list(re.finditer(r'^  ([a-z][a-z-]*):\n', workflow, re.MULTILINE))
    return {
        match.group(1): workflow[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(workflow)
        ]
        for index, match in enumerate(matches)
    }


def _run_tier_assertion(report: Path) -> subprocess.CompletedProcess[bytes]:
    return run_setup(
        [sys.executable, str(_SCRIPT), str(report)],
        check=False,
    )


def _step_blocks(job: str) -> tuple[str, ...]:
    matches = list(re.finditer(r'^      - name: \S.*$', job, re.MULTILINE))
    assert len(matches) == len(re.findall(r'^      - ', job, re.MULTILINE))
    return tuple(
        job[match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(job)]
        for index, match in enumerate(matches)
    )


def _step_containing(steps: tuple[str, ...], fragment: str) -> str:
    matches = [step for step in steps if fragment in step]
    assert len(matches) == 1, fragment
    assert 'if: false' not in matches[0]
    return matches[0]


def _assert_package_and_hosted(jobs: dict[str, str], active: str) -> None:
    package = jobs['package']
    package_steps = _step_blocks(package)
    assert '      - name: Build distributions' in package
    assert '      - name: Smoke-test import' in package
    assert '      - name: Load bundled plugin entry points' in package
    assert 'ubuntu-24.04' in package
    assert 'needs: [quality, test]' in package
    assert 'matrix:' not in package
    for command in (
        'uv venv --python python --no-python-downloads "$RUNNER_TEMP/package-smoke"',
        'sequential_hooks.hosts',
        '"$RUNNER_TEMP/package-smoke/bin/sequential-hooks" --version',
        'git diff --exit-code',
    ):
        assert 'run:' in _step_containing(package_steps, command)
    build = _step_containing(package_steps, 'uv build --python python')
    for flag in (
        '--no-python-downloads',
        '--build-constraints requirements-ci.txt',
        '--require-hashes',
    ):
        assert flag in build
    wheel_install = _step_containing(package_steps, 'Install wheel and runtime dependencies')
    assert '--constraint requirements-ci.txt' in wheel_install
    assert '--build-constraints requirements-ci.txt' in wheel_install
    for command in (
        'sequential_hooks.hosts',
        '"$RUNNER_TEMP/package-smoke/bin/sequential-hooks" --version',
    ):
        assert 'working-directory: ${{ runner.temp }}' in _step_containing(package_steps, command)
    upload = _step_containing(package_steps, 'actions/upload-artifact@')
    assert re.search(r'uses: actions/upload-artifact@[0-9a-f]{40}\s', upload)
    assert "'python-distributions' || 'local-distributions'" in upload
    assert "if: github.event_name != 'pull_request'" in upload

    for job in (jobs['quality'], jobs['test'], package):
        checkout = _step_containing(_step_blocks(job), 'actions/checkout@')
        assert re.search(r'uses: actions/checkout@[0-9a-f]{40}\s', checkout)
        assert 'persist-credentials: false' in checkout
        assert 'runner.environment' in job

    hosted = jobs['hosted-validation']
    assert 'needs: [quality, test, package]' in hosted
    hosted_step = _step_containing(_step_blocks(hosted), 'Require complete hosted CI')
    for result in ('QUALITY_RESULT', 'TEST_RESULT', 'PACKAGE_RESULT'):
        assert f'test "${result}" = success' in hosted_step
    for runner in ('CI_LINUX_RUNNER', 'CI_MACOS_RUNNER', 'CI_WINDOWS_RUNNER'):
        assert runner in active
    assert '        default: true' in active
    assert 'CI_IMPORT: sequential_hooks' in active
    assert 'secrets.' not in active


def _assert_ci_workflow(workflow: str) -> None:
    active = _active_lines(workflow)
    jobs = _job_blocks(active)
    assert set(jobs) == {'quality', 'test', 'package', 'hosted-validation'}
    for job in jobs.values():
        assert not re.search(r'^    name:', job, re.MULTILINE)

    quality = jobs['quality']
    quality_steps = _step_blocks(quality)
    assert 'pytest' not in quality
    assert 'ubuntu-24.04' in quality
    for command in ('make setup', 'make quality', 'git diff --exit-code'):
        assert 'run:' in _step_containing(quality_steps, command)
    install = _step_containing(_step_blocks(jobs['test']), 'uv pip sync --python python')
    for flag in (
        '--no-python-downloads',
        '--build-constraints requirements-ci.txt',
        '--require-hashes',
        'requirements-test.txt',
    ):
        assert flag in install

    tests = jobs['test']
    test_steps = _step_blocks(tests)
    assert '      - name: Record Python environment' in tests
    assert '      - name: Install locked test dependencies' in tests
    assert 'needs: quality' in tests
    assert '    timeout-minutes: 60\n' in tests
    assert 'fail-fast: false' in tests
    cells = re.findall(
        r'^          - os: ([\w.-]+)\n            python-version: "([\d.]+)"$',
        tests,
        re.MULTILINE,
    )
    assert cells == [
        ('ubuntu-24.04', '3.13'),
        ('ubuntu-24.04', '3.14'),
        ('ubuntu-24.04', '3.15'),
        ('macos-15', '3.13'),
        ('windows-2022', '3.13'),
    ]
    setup_python = _step_containing(test_steps, 'actions/setup-python@')
    assert "matrix.os == 'macos-15'" in setup_python
    assert "runner.environment == 'self-hosted' && runner.arch == 'ARM64'" in setup_python
    assert "'arm64' || 'x64'" in setup_python
    suite = _step_containing(test_steps, 'Run suite excluding native tiers')
    assert 'python -m pytest -q -n auto --dist worksteal --durations=15' in suite
    assert '--ignore=tests/containment/test_posix.py' in suite
    assert '--ignore=tests/containment/test_windows_native.py' in suite
    for command in (
        'python -m pytest -q tests/containment/test_posix.py',
        'python -m pytest -q tests/containment/test_windows_native.py',
        'python tests/_assert_ci_tier.py',
    ):
        assert 'run:' in _step_containing(test_steps, command)
    windows_build = _step_containing(test_steps, 'uv build --python python')
    for flag in (
        '--no-python-downloads',
        '--build-constraints requirements-ci.txt',
        '--require-hashes',
        '--wheel',
    ):
        assert flag in windows_build
    assert "if: runner.os == 'Windows'" in windows_build
    windows_launcher = _step_containing(test_steps, "'Scripts' / 'sequential-hooks.exe'")
    assert "if: runner.os == 'Windows'" in windows_launcher
    assert 'run:' in windows_launcher

    _assert_package_and_hosted(jobs, active)


def _write_report(
    path: Path,
    *,
    errors: int = 0,
    failures: int = 0,
    skipped: int = 0,
    tests: int = 16,
) -> None:
    path.write_text(
        '<testsuites><testsuite '
        f'errors="{errors}" failures="{failures}" skipped="{skipped}" '
        f'tests="{tests}" /></testsuites>',
        encoding='ascii',
    )


def test_ci_tier_assertion_accepts_executed_green_tier(tmp_path: Path) -> None:
    report = tmp_path / 'tier.xml'
    _write_report(report)

    result = _run_tier_assertion(report)

    assert result.returncode == 0, process_transcript(result)
    assert (
        decode_text(result.stdout) == 'required tier: tests=16 skipped=0 failures=0 errors=0\n'
    ), process_transcript(result)
    assert decode_text(result.stderr) == '', process_transcript(result)


def test_ci_tier_assertion_accepts_multiple_suites(tmp_path: Path) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        '<testsuites>'
        '<testsuite errors="0" failures="0" skipped="0" tests="5" />'
        '<testsuite errors="0" failures="0" skipped="0" tests="11" />'
        '</testsuites>',
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 0, process_transcript(result)
    assert (
        decode_text(result.stdout) == 'required tier: tests=16 skipped=0 failures=0 errors=0\n'
    ), process_transcript(result)
    assert decode_text(result.stderr) == '', process_transcript(result)


def test_ci_tier_assertion_accepts_single_quoted_suite(tmp_path: Path) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        "<testsuites><testsuite errors='0' failures='0' skipped='0' tests='1' /></testsuites>",
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 0, process_transcript(result)
    assert (
        decode_text(result.stdout) == 'required tier: tests=1 skipped=0 failures=0 errors=0\n'
    ), process_transcript(result)
    assert decode_text(result.stderr) == '', process_transcript(result)


def test_ci_tier_assertion_rejects_duplicate_count_attribute(
    tmp_path: Path,
) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        '<testsuites><testsuite '
        'errors="0" errors="0" failures="0" skipped="0" tests="1" '
        '/></testsuites>',
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr).startswith('invalid required-tier report: '), (
        process_transcript(result)
    )


def test_ci_tier_assertion_rejects_fake_suite_in_comment(tmp_path: Path) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        '<root><!-- <testsuite errors="0" failures="0" skipped="0" tests="1"> --></root>',
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr).startswith('invalid required-tier report: '), (
        process_transcript(result)
    )


@pytest.mark.parametrize(
    'scenario',
    [
        (0, 0, 0, 0, 'tests=0'),
        (16, 1, 0, 0, 'skipped=1'),
        (16, 0, 1, 0, 'failures=1'),
        (16, 0, 0, 1, 'errors=1'),
    ],
)
def test_ci_tier_assertion_rejects_incomplete_tier(
    tmp_path: Path,
    scenario: tuple[int, int, int, int, str],
) -> None:
    tests, skipped, failures, errors, expected = scenario
    report = tmp_path / 'tier.xml'
    _write_report(
        report,
        errors=errors,
        failures=failures,
        skipped=skipped,
        tests=tests,
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 1, process_transcript(result)
    _assert_bounded_stderr(result)
    assert expected in decode_text(result.stderr), process_transcript(result)


def test_ci_tier_assertion_rejects_invalid_xml(tmp_path: Path) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text('<testsuites>', encoding='ascii')

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr).startswith('invalid required-tier report: '), (
        process_transcript(result)
    )


def test_ci_tier_assertion_rejects_long_unreadable_path(tmp_path: Path) -> None:
    report = tmp_path.joinpath(*(['x' * 100] * 6), 'tier.xml')

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr) == 'invalid required-tier report: cannot read report\n', (
        process_transcript(result)
    )


def test_ci_tier_assertion_rejects_missing_count_attribute(
    tmp_path: Path,
) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        '<testsuites><testsuite errors="0" failures="0" skipped="0" /></testsuites>',
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr).startswith('invalid required-tier report: '), (
        process_transcript(result)
    )


def test_ci_tier_assertion_rejects_truncated_matching_suite(
    tmp_path: Path,
) -> None:
    report = tmp_path / 'tier.xml'
    report.write_text(
        '<testsuites><testsuite errors="0" failures="0" skipped="0" tests="1">',
        encoding='ascii',
    )

    result = _run_tier_assertion(report)

    assert result.returncode == 2, process_transcript(result)
    _assert_bounded_stderr(result)
    assert decode_text(result.stderr).startswith('invalid required-tier report: '), (
        process_transcript(result)
    )


@pytest.mark.parametrize('action', ['checkout', 'upload-artifact'])
def test_ci_workflow_accepts_updated_action_pins(action: str) -> None:
    workflow = _WORKFLOW.read_text(encoding='ascii')
    updated = re.sub(rf'(actions/{action}@)[0-9a-f]{{40}}', rf'\g<1>{"a" * 40}', workflow)
    assert updated != workflow
    _assert_ci_workflow(updated)


def test_ci_workflow_preserves_required_checks_and_package_smoke() -> None:
    _assert_ci_workflow(_WORKFLOW.read_text(encoding='ascii'))


@pytest.mark.parametrize(
    ('original', 'replacement'),
    [
        (
            '        run: uv build --python python',
            '        # run: uv build --python python',
        ),
        (
            '          python -m pytest -q -n auto --dist worksteal --durations=15',
            '          # python -m pytest -q -n auto --dist worksteal --durations=15',
        ),
        (
            '          --ignore=tests/containment/test_posix.py',
            '          # --ignore=tests/containment/test_posix.py',
        ),
        (
            '          --ignore=tests/containment/test_windows_native.py',
            '          # --ignore=tests/containment/test_windows_native.py',
        ),
        (
            '          - os: windows-2022\n            python-version: "3.13"',
            '          # - os: windows-2022\n          # python-version: "3.13"',
        ),
        ('          persist-credentials: false', '          # persist-credentials: false'),
        (
            'actions/checkout@',
            'example/checkout@',
        ),
        ('      - name: Run suite excluding native tiers', '      - if: false'),
        (
            '      - name: Build distributions',
            '      - name: Build distributions\n        if: false',
        ),
    ],
)
def test_ci_workflow_rejects_missing_or_disabled_checks(original: str, replacement: str) -> None:
    workflow = _WORKFLOW.read_text(encoding='ascii')
    assert original in workflow
    with pytest.raises(AssertionError):
        _assert_ci_workflow(workflow.replace(original, replacement, 1))
