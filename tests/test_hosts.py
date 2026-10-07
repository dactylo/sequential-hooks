import io
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from sequential_hooks import _cli
from sequential_hooks._doctor import run_doctor
from sequential_hooks._executor import ProcessExecutor
from sequential_hooks._runner import run_pipeline
from sequential_hooks._types import Step
from tests._helpers import CHILD_TIMEOUT_SECONDS, invoke_cli, process_transcript
from tests.hosts_example.host import ExampleHost, ExampleResponse

_POST_INPUT = b'{"tool":"shell","output":"hi"}\n'
_PRE_INPUT = b'{"tool":"shell","arguments":{"command":"echo hi"}}\n'
_PYTHON = Path(sys.executable).name


def _chain(*steps: list[str], event: str = 'BeforeTool', options: Sequence[str] = ()) -> list[str]:
    argv = ['--agent', 'example', '--event', event, *options]
    for index, step in enumerate(steps):
        if index:
            argv.append('--')
        argv.extend(step)
    return argv


def _child(source: str) -> list[str]:
    return [sys.executable, '-c', source]


def _emit(value: object) -> list[str]:
    return _child(f'import json, sys; sys.stdout.write(json.dumps({value!r}))')


def test_doctor_drives_the_example_loader(example_hosts: Path, tmp_path: Path) -> None:
    del example_hosts
    home = tmp_path / 'home'
    (home / '.example').mkdir(parents=True)
    (home / '.example' / 'hooks.json').write_text(
        json.dumps(
            {
                'hooks': [
                    {
                        'event': 'BeforeTool',
                        'command': 'sequential-hooks --agent example --event BeforeTool child',
                    }
                ]
            }
        ),
        encoding='utf-8',
    )
    stream = io.StringIO()

    status = run_doctor(
        'example',
        home=home,
        cwd=tmp_path,
        system_root=tmp_path,
        path_value=str(tmp_path),
        path_ext=None,
        stream=stream,
    )

    report = stream.getvalue()
    assert status == 1
    assert 'agent: example' in report
    source = (home / '.example' / 'hooks.json').as_posix().encode('unicode_escape').decode('ascii')
    assert f'[user] {source}: exists' in report
    assert 'event: BeforeTool' in report
    assert '[info] example_unmatched' in report
    assert 'example hooks registered through environment variables are not inspected' in report
    assert 'host_unavailable' not in report
    assert 'native_event_unsupported' not in report
    assert 'wrong_event' not in report
    assert 'event_missing' not in report
    if sys.platform == 'win32':
        assert (
            '[warning] command_uninspectable: registration command cannot be inspected statically'
        ) in report
    else:
        assert 'command_uninspectable' not in report


def test_example_host_answers_configuration_errors_itself(example_hosts: Path) -> None:
    del example_hosts
    invalid_input = invoke_cli(_chain(_child('pass')), b'{"tool":7}\n')
    bad_syntax = invoke_cli(
        ['--agent', 'example', '--event', 'BeforeTool', '--bogus', 'child'], _PRE_INPUT
    )

    for status, stdout, stderr in (invalid_input, bad_syntax):
        assert status == 0
        assert stdout == b'{"verdict":"deny","reason":"sequential-hooks configuration error"}\n'
        assert 'example: configuration response' in stderr


def test_example_host_deny_stops_the_chain(example_hosts: Path, tmp_path: Path) -> None:
    del example_hosts
    marker = tmp_path / 'ran'
    deny = _emit({'verdict': 'deny', 'reason': 'not today'})
    later = _child(f'from pathlib import Path; Path({str(marker)!r}).write_text("ran")')

    status, stdout, _stderr = invoke_cli(_chain(deny, later), _PRE_INPUT)

    assert status == 0
    assert json.loads(stdout) == {'verdict': 'deny', 'reason': f'[step 1: {_PYTHON}]\nnot today'}
    assert not marker.exists()


def test_example_host_drives_allow_with_replacement(example_hosts: Path) -> None:
    del example_hosts
    first = _emit({'verdict': 'allow', 'arguments': {'command': 'echo rewritten'}, 'note': 'one'})
    second = _child(
        'import json, sys; payload = json.load(sys.stdin); '
        "sys.stdout.write(json.dumps({'note': payload['arguments']['command']}))"
    )

    status, stdout, stderr = invoke_cli(_chain(first, second), _PRE_INPUT)

    assert status == 0
    assert json.loads(stdout) == {
        'verdict': 'allow',
        'arguments': {'command': 'echo rewritten'},
        'notes': f'[step 1: {_PYTHON}]\none\n\n[step 2: {_PYTHON}]\necho rewritten',
    }
    assert stderr == ''


def test_example_host_neutral_children_produce_no_response(example_hosts: Path) -> None:
    del example_hosts
    status, stdout, stderr = invoke_cli(_chain(_child('pass'), _child('pass')), _PRE_INPUT)

    assert (status, stdout, stderr) == (0, b'', '')


def test_example_host_post_tool_deny_blocks(example_hosts: Path) -> None:
    del example_hosts
    status, stdout, _stderr = invoke_cli(
        _chain(_emit({'verdict': 'deny', 'reason': 'undo'}), event='AfterTool'),
        _POST_INPUT,
    )

    assert status == 0
    assert json.loads(stdout) == {'verdict': 'deny', 'reason': f'[step 1: {_PYTHON}]\nundo'}


def test_example_host_timeout_and_output_limit_fail_closed(example_hosts: Path) -> None:
    del example_hosts
    slow = _child('import time; time.sleep(30)')
    loud = _child("import sys; sys.stdout.write('x' * (2 * 1024 * 1024))")

    slow_status, slow_stdout, slow_stderr = invoke_cli(
        _chain(slow, options=['--step-timeout', '0.2']),
        _PRE_INPUT,
    )
    loud_status, loud_stdout, loud_stderr = invoke_cli(_chain(loud), _PRE_INPUT)

    assert slow_status == 0
    assert json.loads(slow_stdout)['verdict'] == 'deny'
    assert 'timeout' in slow_stderr
    assert loud_status == 0
    assert json.loads(loud_stdout)['verdict'] == 'deny'
    assert 'output limit' in loud_stderr


@pytest.mark.parametrize('verdict', [None, 'allow', 'deny'])
def test_real_runner_returns_the_example_response_after_continue_and_stop(
    verdict: str | None,
    tmp_path: Path,
) -> None:
    diagnostics_stream = io.StringIO()
    diagnostics = _cli.Diagnostics(diagnostics_stream)
    spec = ExampleHost()
    adapter = spec.adapter(
        'BeforeTool', _PRE_INPUT, json.loads(_PRE_INPUT), diagnostics, allow_on_failure=False
    )
    marker = tmp_path / 'later-child'
    source = (
        'pass'
        if verdict is None
        else f'import json; print(json.dumps({{"verdict": {verdict!r}}}))'
    )
    later = f'from pathlib import Path; Path({str(marker)!r}).write_text("ran")'
    steps = (Step(1, (sys.executable, '-c', source)), Step(2, (sys.executable, '-c', later)))

    response = run_pipeline(
        adapter, steps, ProcessExecutor(diagnostics), CHILD_TIMEOUT_SECONDS, allow_on_failure=False
    )

    assert isinstance(response, ExampleResponse)
    outcome = subprocess.CompletedProcess(
        '<hook pipeline>', response.exit_code, response.stdout, diagnostics_stream.getvalue()
    )
    assert response.exit_code == 0, process_transcript(outcome)
    assert response.stdout == (b'' if verdict is None else f'{{"verdict":"{verdict}"}}\n'.encode())
    assert marker.exists() is (verdict != 'deny')
