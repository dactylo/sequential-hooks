import json
import runpy
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

_COMMIT = 'a' * 40
_RUN_URL = 'https://example.test/actions/runs/42'
_WORKFLOW = Path(__file__).parents[1] / '.github' / 'workflows' / 'release.yml'


def _verifier() -> str:
    workflow = _WORKFLOW.read_text(encoding='utf-8')
    start = "          python - <<'PY'\n"
    end = '\n          PY'
    assert workflow.count(start) == 1
    script = workflow.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]
    return textwrap.dedent(script)


def _run_verifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runs: list[dict[str, object]],
    tagged_commit: str = _COMMIT,
) -> tuple[str | None, str, str, list[str] | None]:
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1.2.3"\n', encoding='utf-8')
    output = tmp_path / 'github-output'
    summary = tmp_path / 'github-summary'
    verifier = tmp_path / 'verify_release.py'
    output.touch()
    summary.touch()
    verifier.write_text(_verifier(), encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GITHUB_SHA', _COMMIT)
    monkeypatch.setenv('GITHUB_REPOSITORY', 'org/repo')
    monkeypatch.setenv('DEFAULT_BRANCH', 'main')
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))

    query: list[str] | None = None

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ['git', 'rev-parse', '--verify', 'refs/tags/v1.2.3^{commit}']
        assert kwargs == {'capture_output': True, 'text': True}
        return subprocess.CompletedProcess(command, 0, f'{tagged_commit}\n', '')

    def fake_check_output(command: list[str], **kwargs: object) -> str:
        nonlocal query
        query = command
        assert kwargs == {'text': True}
        return json.dumps({'workflow_runs': runs})

    error = None
    with (
        patch('subprocess.run', side_effect=fake_run) as git_run,
        patch('subprocess.check_output', side_effect=fake_check_output) as gh_run,
    ):
        try:
            runpy.run_path(str(verifier), run_name='__main__')
        except SystemExit as exc:
            error = str(exc)
    git_run.assert_called_once()
    if tagged_commit == _COMMIT:
        gh_run.assert_called_once()
    else:
        gh_run.assert_not_called()
    return error, output.read_text(), summary.read_text(), query


def _run(*, status: str = 'completed', conclusion: str = 'success') -> dict[str, object]:
    run: dict[str, object] = {
        'id': 42,
        'status': status,
        'conclusion': conclusion,
        'html_url': _RUN_URL,
    }
    return run


@pytest.mark.parametrize(
    ('runs', 'tagged_commit', 'expected_error'),
    [
        ([], _COMMIT, 'No push CI run exists'),
        ([_run(status='in_progress'), _run()], _COMMIT, 'CI must pass'),
        ([_run(conclusion='failure'), _run()], _COMMIT, 'CI must pass'),
        ([_run()], 'b' * 40, 'v1.2.3 must point to the selected commit'),
    ],
)
def test_ineligible_run_does_not_select_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runs: list[dict[str, object]],
    tagged_commit: str,
    expected_error: str,
) -> None:
    error, output, summary, query = _run_verifier(
        monkeypatch, tmp_path, runs=runs, tagged_commit=tagged_commit
    )

    assert error is not None
    assert expected_error in error
    assert output == ''
    assert summary == ''
    if tagged_commit == _COMMIT:
        assert query is not None
        assert f'head_sha={_COMMIT}' in query
        assert 'branch=main' in query
        assert 'event=push' in query
        assert 'per_page=1' in query
    else:
        assert query is None


def test_successful_push_selects_artifact_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error, output, summary, query = _run_verifier(monkeypatch, tmp_path, runs=[_run()])

    assert error is None
    assert output == 'run-id=42\n'
    assert summary == f'Publishing v1.2.3 from `{_COMMIT}`. [CI run]({_RUN_URL}).\n'
    assert query == [
        'gh',
        'api',
        '--method',
        'GET',
        'repos/org/repo/actions/workflows/ci.yml/runs',
        '-f',
        f'head_sha={_COMMIT}',
        '-f',
        'branch=main',
        '-f',
        'event=push',
        '-f',
        'per_page=1',
    ]
