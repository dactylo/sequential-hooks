import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import (
    invoke_cli,
    process_transcript,
    run_closing_after_first_line,
    run_with_closed_output,
)

_CHILD = Path(__file__).parent / 'fixtures' / 'hook_child.py'


def _child(mode: str, *args: str) -> list[str]:
    return [sys.executable, str(_CHILD), mode, *args]


def _encoded(value: object) -> bytes:
    return f'{json.dumps(value, separators=(",", ":"))}\n'.encode()


def _native_input(host: str, event: str) -> bytes:
    if host == 'agy':
        payload: dict[str, object] = {'stepIdx': 1}
        if event == 'PreToolUse':
            payload['toolCall'] = {'name': 'read_file', 'args': {}}
    else:
        payload = {
            'hook_event_name': event,
            'tool_name': 'Bash',
            'tool_input': {'command': 'original'},
        }
        if event == 'PostToolUse':
            payload['tool_response'] = {}
    return _encoded(payload)


def test_antigravity_explicit_null_post_skips_child(tmp_path: Path) -> None:
    marker = tmp_path / 'explicit-null'

    status, stdout, stderr = invoke_cli(
        [
            '--agent',
            'agy',
            '--event',
            'PostToolUse',
            *_child('write-marker', str(marker), 'ran'),
        ],
        _encoded({'stepIdx': 1, 'toolCall': None}),
    )

    assert status == 0
    assert stdout == b'{}\n'
    assert stderr == ''
    assert not marker.exists()


def test_antigravity_missing_tool_call_post_runs_child(tmp_path: Path) -> None:
    marker = tmp_path / 'missing-tool-call'

    status, stdout, stderr = invoke_cli(
        [
            '--agent',
            'agy',
            '--event',
            'PostToolUse',
            *_child('write-marker', str(marker), 'ran'),
        ],
        _native_input('agy', 'PostToolUse'),
    )

    assert status == 0
    assert stdout == b'{}\n'
    assert stderr == ''
    assert marker.read_text(encoding='utf-8') == 'ran'


def test_antigravity_silent_pre_step_yields_synthetic_allow() -> None:
    status, stdout, _stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PreToolUse', *_child('exit-without-stdin')],
        _native_input('agy', 'PreToolUse'),
    )

    assert status == 0
    assert stdout == b'{"decision":"allow"}\n'


def test_child_stderr_is_labeled_and_never_enters_stdout() -> None:
    status, stdout, stderr = invoke_cli(
        ['--agent', 'agy', '--event', 'PreToolUse', *_child('stderr', 'private detail')],
        _native_input('agy', 'PreToolUse'),
    )

    assert status == 0
    assert stdout == b'{"decision":"allow"}\n'
    assert f'sequential-hooks: step 1 ({Path(sys.executable).name}): private detail' in stderr
    assert b'private detail' not in stdout


@pytest.mark.parametrize('decision', [[], {}])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_container_decision_controls_later_child(
    decision: object,
    allow_on_failure: bool,
    tmp_path: Path,
) -> None:
    marker = tmp_path / 'later'
    invalid = _encoded(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': decision,
            }
        }
    )
    argv = ['--agent', 'claude']
    if allow_on_failure:
        argv.append('--allow-on-failure')
    argv.extend(
        [
            *_child('emit-hex', invalid.hex()),
            '--',
            *_child('write-marker', str(marker), 'ran'),
        ]
    )

    status, stdout, stderr = invoke_cli(argv, _native_input('claude', 'PreToolUse'))

    assert status == 0
    assert stdout.count(b'\n') == 1
    response = json.loads(stdout)
    if allow_on_failure:
        assert marker.read_text(encoding='utf-8') == 'ran'
    else:
        assert not marker.exists()
        assert response['hookSpecificOutput']['permissionDecision'] == 'deny'
    diagnostic = 'unsupported: unsupported PreToolUse permissionDecision'
    assert diagnostic.encode() in stdout
    assert diagnostic in stderr
    assert b'internal invariant failure' not in stdout
    assert 'internal invariant failure' not in stderr
    assert b'unexpected internal failure' not in stdout
    assert 'unexpected internal failure' not in stderr


class TestClosedOutput:
    @staticmethod
    def _run(case: str) -> subprocess.CompletedProcess[bytes]:
        if case == 'help-after-first-line':
            return run_closing_after_first_line(['--help'])
        if case == 'help':
            return run_with_closed_output(['--help'])
        depth = 600
        payload = (
            b'{"hook_event_name":"PreToolUse","tool_name":"probe","tool_input":'
            + b'{"k":' * depth
            + b'{}'
            + b'}' * depth
            + b'}'
        )
        return run_with_closed_output(['--agent', 'claude', 'unused-child'], stdin=payload)

    @pytest.mark.parametrize('case', ['hook-response', 'help', 'help-after-first-line'])
    def test_closure_is_quiet(self, case: str) -> None:
        result = self._run(case)
        transcript = process_transcript(result)
        assert b'Traceback' not in result.stderr, transcript
        assert b'Exception ignored' not in result.stderr, transcript

    @pytest.mark.parametrize('case', ['hook-response', 'help', 'help-after-first-line'])
    def test_closure_preserves_status(self, case: str) -> None:
        result = self._run(case)
        assert result.returncode == 0, process_transcript(result)

    def test_diagnostics_closed_stderr_preserves_status(self) -> None:
        result = run_with_closed_output(['--agent', 'missing', 'child'], closed='stderr')
        assert result.returncode == 2, process_transcript(result)
        assert result.stdout == b'', process_transcript(result)


def test_claude_pre_replacement_reaches_next_step() -> None:
    first = _encoded(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'updatedInput': {'command': 'replacement'},
            }
        }
    )
    second = _encoded(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
            }
        }
    )
    argv = [
        '--agent',
        'claude',
        *_child('emit-hex', first.hex()),
        '--',
        *_child('validate-json-field', 'tool_input.command', '"replacement"', second.hex()),
    ]

    status, stdout, _stderr = invoke_cli(argv, _native_input('claude', 'PreToolUse'))

    assert status == 0
    response = json.loads(stdout)
    assert response['hookSpecificOutput']['updatedInput'] == {'command': 'replacement'}


def test_codex_terminal_pre_deny_prevents_later_marker(tmp_path: Path) -> None:
    marker = tmp_path / 'later'
    deny = _encoded(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': 'stop',
            }
        }
    )
    argv = [
        '--agent',
        'codex',
        *_child('emit-hex', deny.hex()),
        '--',
        *_child('write-marker', str(marker), 'ran'),
    ]

    status, stdout, _stderr = invoke_cli(argv, _native_input('codex', 'PreToolUse'))

    assert status == 0
    assert json.loads(stdout) == {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': f'[step 1: {Path(sys.executable).name}]\nstop',
        }
    }
    assert not marker.exists()


def test_hook_mode_does_not_persist_native_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())

    status, _stdout, _stderr = invoke_cli(
        ['--agent', 'claude', *_child('exit-without-stdin')],
        _native_input('claude', 'PostToolUse'),
    )

    assert status == 0
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize('host', ['claude', 'codex', 'agy'])
def test_post_infrastructure_failure_permits_later_marker(
    host: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / host
    argv = ['--agent', host]
    if host == 'agy':
        argv.extend(['--event', 'PostToolUse'])
    argv.extend(
        [
            str(tmp_path / 'missing-child'),
            '--',
            *_child('write-marker', str(marker), 'ran'),
        ]
    )

    status, stdout, _stderr = invoke_cli(argv, _native_input(host, 'PostToolUse'))

    assert status == 0
    assert marker.read_text(encoding='utf-8') == 'ran'
    missing_name = 'missing-child'
    if host == 'claude':
        assert json.loads(stdout) == {
            'systemMessage': (
                f'[step 1: {missing_name}]\nnot found: step 1 ({missing_name}) not found'
            )
        }
    elif host == 'codex':
        assert json.loads(stdout) == {
            'systemMessage': (
                f'[step 1: {missing_name}]\n'
                'not found: child result rejected; see developer diagnostics'
            )
        }
    else:
        assert stdout == b'{}\n'


def test_shell_metacharacters_remain_literal(tmp_path: Path) -> None:
    marker = tmp_path / 'unintended'
    metacharacter = f'; touch {marker}'
    child_response = b'{"decision":"allow"}\n'

    status, stdout, stderr = invoke_cli(
        [
            '--agent',
            'agy',
            '--event',
            'PreToolUse',
            *_child(
                'validate-argv',
                json.dumps([metacharacter]),
                child_response.hex(),
                metacharacter,
            ),
        ],
        _native_input('agy', 'PreToolUse'),
    )

    assert status == 0
    assert stdout == child_response
    assert stderr == ''
    assert not marker.exists()
