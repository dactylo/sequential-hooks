# pyright: reportPrivateUsage=false

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import sequential_hooks.hosts._skeleton as skeleton_module
from plugins.claude._sequential_hooks._adapter import (
    _CLAUDE_FIELD_LIMIT_CHARACTERS,
    ClaudeAdapter,
    ClaudeHost,
)
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._executor import _STDOUT_LIMIT_BYTES, ProcessExecutor
from sequential_hooks._runner import run_pipeline
from sequential_hooks._types import ContainmentState, FailureKind, Step, StepFailure, StepResult
from sequential_hooks.hosts import (
    API_VERSION,
    FINAL_RESPONSE_LIMIT_BYTES,
    TRUNCATION_MARKER,
    AdapterOutputError,
    ChainControl,
    JsonObject,
    JsonValue,
    OutputFailureKind,
    encode_plain_response,
    labeled,
)
from sequential_hooks.hosts._json import load_object

_CLAUDE_PRE_RANK: dict[str, int] = {
    'neutral': 0,
    'allow': 1,
    'ask': 2,
    'defer': 3,
    'deny': 4,
}
_FIXTURE_ROOT = Path(__file__).parent / 'fixtures'


class _QueueExecutor(ProcessExecutor):
    def __init__(self, results: list[StepResult]) -> None:
        self.calls: list[Step] = []
        self.results = results

    def run(
        self,
        step: Step,
        input_bytes: bytes,
        timeout: float | None,
        *,
        allow_uncontained: bool,
    ) -> StepResult:
        del input_bytes, timeout, allow_uncontained
        self.calls.append(step)
        return self.results.pop(0)


def _additional_context_output(text: str) -> JsonObject:
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'additionalContext': text,
        }
    }


def _assert_minimal_oversized_failure(
    adapter: ClaudeAdapter,
    control: ChainControl,
    stream: io.StringIO,
    failing_step: Step,
    *,
    expected_stop: bool,
) -> None:
    assert (control == 'stop') is expected_stop
    response_bytes = adapter.finalize().stdout
    response = load_object(response_bytes)
    if expected_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        user_text = cast('str', hook['permissionDecisionReason'])
    else:
        user_text = cast('str', response['systemMessage'])
    assert 'oversized' in user_text
    assert 'developer diagnostics' in user_text
    assert len(user_text) < 200
    assert len(response_bytes) <= FINAL_RESPONSE_LIMIT_BYTES
    diagnostic = stream.getvalue()
    assert f'step {failing_step.index} (' in diagnostic
    assert (
        'oversized: labeled contributions cannot fit within the response field limit' in diagnostic
    )


def _chunked_mutation(character_count: int) -> JsonObject:
    chunks: list[JsonValue] = []
    remaining = character_count
    while remaining:
        size = min(9_000, remaining)
        chunks.append('m' * size)
        remaining -= size
    return {'chunks': chunks}


def _decision_reason_output(text: str) -> JsonObject:
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'ask',
            'permissionDecisionReason': text,
        }
    }


def _deep_mutation() -> JsonObject:
    root: JsonObject = {}
    cursor = root
    for _index in range(sys.getrecursionlimit() + 100):
        child: JsonObject = {}
        cursor['next'] = child
        cursor = child
    return root


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURE_ROOT / name).read_bytes()


def _make_adapter_with_stream(
    raw_input: bytes,
    *,
    allow_on_failure: bool = False,
) -> tuple[ClaudeAdapter, io.StringIO]:
    payload = load_object(raw_input)
    stream = io.StringIO()
    adapter = ClaudeAdapter(
        raw_input,
        payload,
        Diagnostics(stream),
        allow_on_failure=allow_on_failure,
    )
    return adapter, stream


def _make_adapter(
    raw_input: bytes,
    *,
    allow_on_failure: bool = False,
) -> ClaudeAdapter:
    adapter, _stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    return adapter


def _mcp_post_input() -> bytes:
    payload = load_object(_fixture_bytes('claude_post.json'))
    payload['tool_name'] = 'mcp__server__tool'
    return f'{json.dumps(payload, separators=(",", ":"))}\n'.encode()


def _native_block_boundary_mutation() -> JsonObject:
    return _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 490)


def _near_budget_mutation() -> JsonObject:
    return _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 750)


def _output(
    stdout: bytes = b'',
    *,
    returncode: int = 0,
    blocking_stderr: str = '',
) -> StepResult:
    assert len(stdout) <= _STDOUT_LIMIT_BYTES
    return StepResult(returncode, stdout, blocking_stderr)


def _protocol_output(value: JsonObject) -> StepResult:
    return _output(f'{json.dumps(value, separators=(",", ":"))}\n'.encode())


def _post_protocol(
    *,
    additional_context: str | None = None,
    updated_mcp_output: object = ...,
    updated_output: object = ...,
) -> StepResult:
    hook: JsonObject = {'hookEventName': 'PostToolUse'}
    if additional_context is not None:
        hook['additionalContext'] = additional_context
    if updated_output is not ...:
        hook['updatedToolOutput'] = cast('JsonValue', updated_output)
    if updated_mcp_output is not ...:
        hook['updatedMCPToolOutput'] = cast('JsonValue', updated_mcp_output)
    return _protocol_output({'hookSpecificOutput': hook})


def _pre_protocol(
    decision: str,
    *,
    additional_context: str | None = None,
    reason: str | None = None,
    updated_input: object = ...,
) -> StepResult:
    hook: JsonObject = {
        'hookEventName': 'PreToolUse',
        'permissionDecision': decision,
    }
    if additional_context is not None:
        hook['additionalContext'] = additional_context
    if reason is not None:
        hook['permissionDecisionReason'] = reason
    if updated_input is not ...:
        hook['updatedInput'] = cast('JsonValue', updated_input)
    return _protocol_output({'hookSpecificOutput': hook})


def _step(index: int, name: str | None = None) -> Step:
    return Step(index, (name or f'step-{index}',))


def _stop_reason_output(text: str) -> JsonObject:
    return {'continue': False, 'stopReason': text}


def _system_message_output(text: str) -> JsonObject:
    return {'systemMessage': text}


def _terminal_boundary_mutation() -> JsonObject:
    return _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 475)


def _tight_budget_mutation() -> JsonObject:
    return _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 500)


def test_claude_accepted_duplicate_terminal_sequence_diagnoses_once() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    adapter.consume(_step(1), _protocol_output({'terminalSequence': 'first'}))

    control = adapter.consume(
        _step(2),
        _protocol_output({'terminalSequence': 'second'}),
    )
    adapter.finalize()
    adapter.finalize()

    assert control == 'continue'
    assert stream.getvalue().count('earlier step supplied one') == 1


@pytest.mark.parametrize(
    ('fixture_name', 'event'),
    [
        ('claude_pre.json', 'PreToolUse'),
        ('claude_post.json', 'PostToolUse'),
    ],
)
def test_claude_adapter_accepts_realistic_strict_input(
    fixture_name: str,
    event: str,
) -> None:
    raw_input = _fixture_bytes(fixture_name)

    adapter = _make_adapter(raw_input)

    assert adapter.event == event
    assert adapter.should_run_steps is True
    assert adapter.step_input() == raw_input


@pytest.mark.parametrize(
    'raw_input',
    [
        b'{}',
        b'{"hook_event_name":"PreToolUse","tool_input":{}}',
        b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{}}',
        b'{"hook_event_name":1,"tool_name":"Bash","tool_input":{}}',
        b'{"hook_event_name":"PreToolUse","tool_name":"","tool_input":{}}',
        b'{"hook_event_name":"PreToolUse","tool_name":1,"tool_input":{}}',
        b'{"hook_event_name":"Other","tool_name":"Bash","tool_input":{}}',
    ],
)
def test_claude_adapter_rejects_invalid_required_input(raw_input: bytes) -> None:
    with pytest.raises(ValueError, match='Claude Code'):
        _make_adapter(raw_input)


@pytest.mark.parametrize('decision', ['allow', 'ask'])
def test_claude_allow_or_ask_retains_context_and_last_replacement(
    decision: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    replacement = {'command': 'safe'}

    adapter.consume(
        _step(1),
        _pre_protocol(
            decision,
            additional_context='context',
            updated_input=replacement,
        ),
    )

    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['additionalContext'] == '[step 1: step-1]\ncontext'
    assert hook['updatedInput'] == replacement


@pytest.mark.parametrize(
    'stdout',
    [
        b'\r{"systemMessage":"parsed"}',
        b'{"systemMessage":"parsed"}\r',
        b'\r{"systemMessage":"parsed"}\r',
    ],
)
def test_claude_carriage_return_wrapped_json_is_structured(stdout: bytes) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _output(stdout))

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    assert response['systemMessage'] == '[step 1: step-1]\nparsed'


def test_claude_continue_false_is_terminal_with_owned_stop_reason() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output({'continue': False, 'stopReason': 'stop globally'}),
    )

    assert control == 'stop'
    assert load_object(adapter.finalize().stdout) == {
        'continue': False,
        'stopReason': '[step 1: step-1]\nstop globally',
    }


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_continue_false_without_reason_remains_terminal_in_response(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))

    control = adapter.consume(_step(1), _protocol_output({'continue': False}))

    assert control == 'stop'
    assert load_object(adapter.finalize().stdout) == {'continue': False}


def test_claude_continue_true_is_neutral() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _protocol_output({'continue': True}))

    assert control == 'continue'
    assert adapter.finalize().stdout == b''


def test_claude_current_and_deprecated_pre_tool_forms_are_ambiguous() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output(
            {
                'decision': 'approve',
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow',
                },
            }
        ),
    )

    assert control == 'stop'
    assert b'unsupported' in adapter.finalize().stdout


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_current_pre_tool_rejects_orphan_legacy_reason(
    allow_on_failure: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=allow_on_failure,
    )
    prior_input = adapter.step_input()

    control = adapter.consume(
        _step(1),
        _protocol_output(
            {
                'reason': 'orphan legacy reason',
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow',
                    'updatedInput': {'safe': True},
                },
            }
        ),
    )

    assert control == ('continue' if allow_on_failure else 'stop')
    assert adapter.step_input() == prior_input
    assert 'reason requires a deprecated decision' in stream.getvalue()


def test_claude_deep_mutation_copy_propagates_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    prior_input = adapter.step_input()
    prior_response = adapter.finalize()
    assert adapter.step_input() == prior_input
    assert adapter.finalize() == prior_response
    mutation = _deep_mutation()

    def load_deep_mutation(_stdout: bytes) -> JsonObject:
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'updatedInput': mutation,
            }
        }

    monkeypatch.setattr(skeleton_module, 'load_object', load_deep_mutation)

    with pytest.raises(RecursionError):
        adapter.consume(_step(1), _output(b'ignored'))

    assert adapter.step_input() == prior_input
    assert adapter.finalize() == prior_response


@pytest.mark.parametrize(
    ('legacy', 'canonical'),
    [
        ({'decision': 'approve'}, 'allow'),
        ({'decision': 'block', 'reason': 'legacy block'}, 'deny'),
    ],
)
def test_claude_deprecated_pre_tool_decision_normalizes_to_current_shape(
    legacy: JsonObject,
    canonical: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    adapter.consume(_step(1), _protocol_output(legacy))

    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == canonical
    if canonical == 'deny':
        assert 'legacy block' in cast('str', hook['permissionDecisionReason'])


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('empty_first', [False, True], ids=['empty-last', 'empty-first'])
def test_claude_empty_object_does_not_discard_plain_output(
    event_fixture: str,
    *,
    empty_first: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes(event_fixture))
    results = (
        (_protocol_output({}), _output(b'native plain'))
        if empty_first
        else (_output(b'native plain'), _protocol_output({}))
    )

    controls = [adapter.consume(_step(index), result) for index, result in enumerate(results, 1)]

    assert all((control == 'continue') for control in controls)
    assert adapter.finalize().stdout == b'native plain\n'
    assert stream.getvalue() == ''


def test_claude_empty_stdout_is_neutral() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _output())

    assert control == 'continue'
    assert adapter.finalize().stdout == b''


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_exit_two_is_terminal_native_feedback(event_fixture: str) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))

    control = adapter.consume(
        _step(1, 'blocker'),
        _output(returncode=2, blocking_stderr='native feedback'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    if event_fixture == 'claude_pre.json':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert 'native feedback' in cast('str', hook['permissionDecisionReason'])
    else:
        assert response['decision'] == 'block'
        assert 'native feedback' in cast('str', response['reason'])
    assert 'systemMessage' not in response


@pytest.mark.parametrize(
    ('stdout', 'expected_class'),
    [
        (b'{malformed', 'malformed'),
        (b'  {malformed', 'malformed'),
        (b'\t\t{malformed', 'malformed'),
        (b'\r\r{malformed', 'malformed'),
        (b'\n\n{malformed', 'malformed'),
        (b' \t\r\n{malformed', 'malformed'),
        (b'{malformed-one\n{malformed-two', 'malformed'),
        (b'{malformed\nplain text', 'malformed'),
        (b'plain text\n{malformed', 'plain'),
        (
            b'{malformed\n{"systemMessage":"gate-b-valid"}',
            'malformed',
        ),
        (
            b'{"systemMessage":"gate-b-valid"}\n{malformed',
            'malformed',
        ),
    ],
)
@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_exit_zero_parser_boundary_matches_live_host(
    event_fixture: str,
    allow_on_failure: bool,
    stdout: bytes,
    expected_class: str,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(_step(1, 'gate-b'), _output(stdout))

    should_stop = (
        expected_class == 'malformed'
        and event_fixture == 'claude_pre.json'
        and not allow_on_failure
    )
    assert (control == 'stop') is should_stop
    response_bytes = adapter.finalize().stdout
    if expected_class == 'plain':
        assert response_bytes == stdout + b'\n'
        assert stream.getvalue() == ''
    else:
        assert stdout not in response_bytes
        assert b'malformed' in response_bytes
        assert 'step 1 (gate-b): malformed' in stream.getvalue()


def test_claude_failed_mutation_publication_restores_exact_prior_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_input = (
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash",'
        b'"tool_input":{"command":"original"},"unknown":"\\ud800"}\n'
    )
    adapter = _make_adapter(raw_input)
    prior_result = _protocol_output(
        {
            'systemMessage': 'prior message',
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'permissionDecisionReason': 'prior decision',
                'additionalContext': 'prior context',
            },
        }
    )
    assert adapter.consume(_step(1, 'prior'), prior_result) == 'continue'
    prior_state = vars(adapter.state)
    prior_payload = prior_state['payload']
    prior_raw_input = adapter.step_input()
    prior_contexts = list(cast('list[str]', prior_state['contexts']))
    prior_messages = list(cast('list[str]', prior_state['messages']))
    prior_decision = prior_state['decision']
    prior_reasons = list(cast('list[str]', prior_state['decision_reasons']))
    prior_response = adapter.finalize().stdout

    def reject_failure(
        failure_step: Step,
        failure: str,
        message: str,
        *,
        uncontained: bool = False,
    ) -> ChainControl:
        del failure_step, uncontained
        raise AdapterOutputError(OutputFailureKind(failure), message)

    monkeypatch.setattr(adapter, '_apply_failure', reject_failure)
    candidate = _protocol_output(
        {
            'systemMessage': 'tentative message',
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'permissionDecisionReason': 'tentative decision',
                'additionalContext': 'tentative context',
                'updatedInput': {'command': 'replacement'},
            },
        }
    )

    with pytest.raises(AdapterOutputError) as raised:
        adapter.consume(_step(2, 'mutator'), candidate)

    assert raised.value.kind is OutputFailureKind.MALFORMED
    restored = vars(adapter.state)
    assert restored['payload'] == prior_payload
    assert adapter.step_input() is prior_raw_input
    assert restored['contexts'] == prior_contexts
    assert restored['messages'] == prior_messages
    assert restored['decision'] == prior_decision
    assert restored['decision_reasons'] == prior_reasons
    assert adapter.finalize().stdout is prior_response


def test_claude_failure_diagnoses_earlier_plain_output_being_discarded() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    adapter.consume(_step(1, 'plain'), _output(b'native plain'))

    control = adapter.consume(
        _step(2, 'failed'),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timed out'),
        ),
    )

    assert control == 'continue'
    assert b'native plain' not in adapter.finalize().stdout
    assert 'plain stdout omitted' in stream.getvalue()
    assert "'native plain'" in stream.getvalue()
    assert 'because a step failure ended the chain' in stream.getvalue()
    assert 'because structured output survived' not in stream.getvalue()


def test_claude_failure_recomputes_shedding_after_an_accepted_mutation() -> None:
    adapter = _make_adapter(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=True,
    )
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 6_000)
    adapter.consume(
        _step(1),
        _pre_protocol(
            'ask',
            reason='r' * 1_000,
            updated_input=mutation,
        ),
    )

    control = adapter.consume(
        _step(2),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 't' * 5_000),
        ),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert response['systemMessage'] == (
        'timeout: sequential-hooks cannot represent hook output; see developer diagnostics'
    )
    assert hook['updatedInput'] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_final_budget_sheds_context_before_messages() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500)
    adapter.consume(
        _step(1),
        _pre_protocol(
            'allow',
            additional_context='c' * 6_000,
            updated_input=mutation,
        ),
    )

    control = adapter.consume(
        _step(2),
        _protocol_output({'systemMessage': 'm' * 3_000}),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert 'additionalContext' not in hook
    assert response['systemMessage'] == labeled(_step(2), 'm' * 3_000)
    assert hook['updatedInput'] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_final_budget_sheds_messages_before_winning_reason() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 6_000)
    adapter.consume(
        _step(1),
        _pre_protocol(
            'ask',
            reason='r' * 1_000,
            updated_input=mutation,
        ),
    )

    control = adapter.consume(
        _step(2),
        _protocol_output({'systemMessage': 'm' * 5_000}),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert 'systemMessage' not in response
    assert hook['permissionDecisionReason'] == labeled(_step(1), 'r' * 1_000)
    assert hook['updatedInput'] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_final_budget_truncates_winning_reason_last() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 1_500)
    adapter.consume(_step(1), _pre_protocol('ask', reason='r' * 5_000))

    control = adapter.consume(
        _step(2),
        _pre_protocol(
            'ask',
            updated_input=mutation,
        ),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    reason = cast('str', hook['permissionDecisionReason'])
    assert reason.startswith('[step 1: step-1]\n')
    assert reason.endswith('[truncated by sequential-hooks]')
    assert hook['updatedInput'] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_final_defer_omits_context_and_replacement() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    adapter.consume(
        _step(1),
        _pre_protocol(
            'allow',
            additional_context='omit me',
            updated_input={'command': 'omit me'},
        ),
    )

    adapter.consume(_step(2), _pre_protocol('defer', reason='deferred'))

    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecision'] == 'defer'
    assert 'additionalContext' not in hook
    assert 'updatedInput' not in hook


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_final_fit_attributes_current_mutation_as_undeliverable(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    adapter.consume(
        _step(1, 'terminal'),
        _protocol_output({'terminalSequence': 't' * 1_000}),
    )
    mutation = _near_budget_mutation()
    result = (
        _pre_protocol('allow', updated_input=mutation)
        if event_fixture == 'claude_pre.json'
        else _post_protocol(updated_output=mutation)
    )

    control = adapter.consume(_step(2, 'mutator'), result)

    should_stop = event_fixture == 'claude_pre.json'
    assert (control == 'stop') is should_stop
    response_bytes = adapter.finalize().stdout
    assert b'undeliverable mutation' in response_bytes
    assert 'undeliverable mutation' in stream.getvalue()
    assert adapter.step_input() == raw_input


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_final_fit_attributes_later_terminal_sequence_as_oversized(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    mutation = _near_budget_mutation()
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    initial = (
        _pre_protocol('allow', updated_input=mutation)
        if event_fixture == 'claude_pre.json'
        else _post_protocol(updated_output=mutation)
    )
    adapter.consume(_step(1, 'mutator'), initial)
    terminal_sequence = 't' * 1_000

    control = adapter.consume(
        _step(2, 'terminal'),
        _protocol_output({'terminalSequence': terminal_sequence}),
    )

    should_stop = event_fixture == 'claude_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response_bytes = adapter.finalize().stdout
    response = load_object(response_bytes)
    assert 'terminalSequence' not in response
    assert terminal_sequence.encode() not in response_bytes
    assert 'oversized' in stream.getvalue()
    assert 'undeliverable mutation' not in stream.getvalue()
    input_payload = load_object(adapter.step_input())
    input_field = 'tool_input' if event_fixture == 'claude_pre.json' else 'tool_response'
    assert input_payload[input_field] == mutation
    if not should_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        mutation_field = (
            'updatedInput' if event_fixture == 'claude_pre.json' else 'updatedToolOutput'
        )
        assert hook[mutation_field] == mutation


def test_claude_final_fit_preserves_all_reason_labels_at_marker_boundary() -> None:
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 578)
    adapter = _make_adapter(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=True,
    )
    first_reason_step = _step(2, 'first-reason')
    second_reason_step = _step(3, 'second-reason-label')
    adapter.consume(
        _step(1, 'mutator'),
        _pre_protocol('allow', updated_input=mutation),
    )
    adapter.consume(
        first_reason_step,
        _pre_protocol('ask', reason='r' * _CLAUDE_FIELD_LIMIT_CHARACTERS),
    )
    first_label = labeled(first_reason_step, '').removesuffix('\n')
    second_label = labeled(second_reason_step, '').removesuffix('\n')
    expected = f'{first_label}\n\n{second_label}{TRUNCATION_MARKER}'

    control = adapter.consume(
        second_reason_step,
        _pre_protocol('ask', reason='s' * _CLAUDE_FIELD_LIMIT_CHARACTERS),
    )

    assert control == 'continue'
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecisionReason'] == expected
    assert len(adapter.finalize().stdout) == FINAL_RESPONSE_LIMIT_BYTES


def test_claude_final_fit_preserves_complete_winning_reason_label() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    mutation = _near_budget_mutation()
    adapter.consume(
        _step(1, 'mutator'),
        _pre_protocol('allow', updated_input=mutation),
    )

    control = adapter.consume(
        _step(2, 'reason-owner'),
        _pre_protocol(
            'ask',
            reason='r' * _CLAUDE_FIELD_LIMIT_CHARACTERS,
        ),
    )

    assert control == 'continue'
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    reason = cast('str', hook['permissionDecisionReason'])
    assert reason.startswith('[step 2: reason-owner]\n')
    assert reason.endswith('[truncated by sequential-hooks]')
    assert hook['updatedInput'] == mutation


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_final_fit_truncates_stop_reason_and_preserves_terminal(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    mutation = _near_budget_mutation()
    adapter = _make_adapter(raw_input, allow_on_failure=allow_on_failure)
    initial = (
        _pre_protocol('allow', updated_input=mutation)
        if event_fixture == 'claude_pre.json'
        else _post_protocol(updated_output=mutation)
    )
    assert adapter.consume(_step(1, 'mutator'), initial) == 'continue'

    control = adapter.consume(
        _step(2, 'terminal'),
        _protocol_output(
            {
                'continue': False,
                'stopReason': 's' * _CLAUDE_FIELD_LIMIT_CHARACTERS,
            }
        ),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    stop_reason = cast('str', response['stopReason'])
    assert response['continue'] is False
    assert stop_reason.startswith('[step 2: terminal]\n')
    assert stop_reason.endswith('[truncated by sequential-hooks]')
    assert len(stop_reason) < _CLAUDE_FIELD_LIMIT_CHARACTERS
    input_payload = load_object(adapter.step_input())
    input_field = 'tool_input' if event_fixture == 'claude_pre.json' else 'tool_response'
    assert input_payload[input_field] == mutation
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    mutation_field = 'updatedInput' if event_fixture == 'claude_pre.json' else 'updatedToolOutput'
    assert hook[mutation_field] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_first_terminal_sequence_wins_without_truncation() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    first = '\x1b[?25l'
    second = '\x1b[?25h'
    adapter.consume(_step(1), _protocol_output({'terminalSequence': first}))

    control = adapter.consume(_step(2), _protocol_output({'terminalSequence': second}))

    assert control == 'continue'
    assert load_object(adapter.finalize().stdout)['terminalSequence'] == first
    assert 'terminalSequence' in stream.getvalue()


@pytest.mark.parametrize(
    ('event_fixture', 'mutation_field', 'state_field'),
    [
        ('claude_pre.json', 'updatedInput', 'updated_input'),
        ('claude_post.json', 'updatedToolOutput', 'updated_output'),
    ],
)
def test_claude_fitting_borrows_accepted_mutation(
    event_fixture: str,
    mutation_field: str,
    state_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500)
    initial = (
        _pre_protocol(
            'allow',
            additional_context='c' * 6_000,
            updated_input=mutation,
        )
        if event_fixture == 'claude_pre.json'
        else _post_protocol(
            additional_context='c' * 6_000,
            updated_output=mutation,
        )
    )
    assert adapter.consume(_step(1), initial) == 'continue'
    next_input = adapter.step_input()
    owned_mutation = vars(adapter.state)[state_field]
    deepcopy = skeleton_module.deepcopy
    mutation_copy_count = 0

    def counted_deepcopy(value: object) -> object:
        nonlocal mutation_copy_count
        if value is owned_mutation:
            mutation_copy_count += 1
        return deepcopy(value)

    monkeypatch.setattr(skeleton_module, 'deepcopy', counted_deepcopy)

    control = adapter.consume(
        _step(2),
        _protocol_output({'systemMessage': 'm' * 3_000}),
    )

    assert control == 'continue'
    response_bytes = adapter.finalize().stdout
    response = load_object(response_bytes)
    hook: JsonObject = {
        'hookEventName': ('PreToolUse' if event_fixture == 'claude_pre.json' else 'PostToolUse'),
        mutation_field: mutation,
    }
    if event_fixture == 'claude_pre.json':
        hook['permissionDecision'] = 'allow'
    assert mutation_copy_count == 0
    assert adapter.step_input() == next_input
    assert response == {
        'systemMessage': labeled(_step(2), 'm' * 3_000),
        'hookSpecificOutput': hook,
    }
    assert len(response_bytes) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_fitting_builds_and_encodes_each_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    mutation = _chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500)
    adapter.consume(
        _step(1),
        _pre_protocol(
            'allow',
            additional_context='c' * 6_000,
            updated_input=mutation,
        ),
    )
    counts = {'build': 0, 'encode': 0}
    final_object = ClaudeAdapter.build_response
    encode_response = skeleton_module.encode_response

    def counted_final_object(self: ClaudeAdapter) -> JsonObject:
        counts['build'] += 1
        return final_object(self)

    def counted_encode_response(response: JsonObject) -> bytes:
        counts['encode'] += 1
        return encode_response(response)

    monkeypatch.setattr(ClaudeAdapter, 'build_response', counted_final_object)
    monkeypatch.setattr(skeleton_module, 'encode_response', counted_encode_response)

    control = adapter.consume(
        _step(2),
        _protocol_output({'systemMessage': 'm' * 3_000}),
    )

    assert control == 'continue'
    assert counts == {'build': 2, 'encode': 2}
    accepted_response = cast('bytes', vars(adapter.state)['response_bytes'])
    assert adapter.finalize().stdout is accepted_response
    assert counts == {'build': 2, 'encode': 2}


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_hook_specific_event_mismatch_follows_failure_policy(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))
    wrong_event = 'PostToolUse' if event_fixture == 'claude_pre.json' else 'PreToolUse'

    control = adapter.consume(
        _step(1),
        _protocol_output({'hookSpecificOutput': {'hookEventName': wrong_event}}),
    )

    if event_fixture == 'claude_pre.json':
        assert control == 'stop'
    else:
        assert control == 'continue'
        assert 'unsupported' in cast(
            'str',
            load_object(adapter.finalize().stdout)['systemMessage'],
        )


@pytest.mark.parametrize('decision', [[], {}])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_container_pre_decision_follows_failure_policy(
    decision: JsonValue,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes('claude_pre.json')
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    result = _protocol_output(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': decision,
            }
        }
    )

    control = adapter.consume(_step(1, 'invalid'), result)

    assert control == ('continue' if allow_on_failure else 'stop')
    assert adapter.step_input() == raw_input
    response_bytes = adapter.finalize().stdout
    response = load_object(response_bytes)
    if allow_on_failure:
        assert adapter.consume(_step(2), _pre_protocol('allow')) == 'continue'
        response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == ('allow' if allow_on_failure else 'deny')
    diagnostic = 'unsupported: unsupported PreToolUse permissionDecision'
    assert diagnostic.encode() in response_bytes
    assert diagnostic in stream.getvalue()


@pytest.mark.parametrize(
    ('kind', 'builder', 'response_path'),
    [
        (
            'systemMessage',
            _system_message_output,
            ('systemMessage',),
        ),
        (
            'additionalContext',
            _additional_context_output,
            ('hookSpecificOutput', 'additionalContext'),
        ),
        (
            'permissionDecisionReason',
            _decision_reason_output,
            ('hookSpecificOutput', 'permissionDecisionReason'),
        ),
        (
            'stopReason',
            _stop_reason_output,
            ('stopReason',),
        ),
    ],
)
def test_claude_human_field_limit_includes_label_and_marker(
    kind: str,
    builder: Callable[[str], JsonObject],
    response_path: tuple[str, ...],
) -> None:
    del kind
    label_characters = len(labeled(_step(1), ''))
    exact_text = 'x' * (_CLAUDE_FIELD_LIMIT_CHARACTERS - label_characters)
    over_text = f'{exact_text}x'

    exact_adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    exact_adapter.consume(
        _step(1),
        _protocol_output(builder(exact_text)),
    )
    exact_bytes = exact_adapter.finalize().stdout
    exact_value: object = load_object(exact_bytes)
    for field in response_path:
        exact_value = cast('dict[str, object]', exact_value)[field]

    over_adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    over_adapter.consume(
        _step(1),
        _protocol_output(builder(over_text)),
    )
    over_bytes = over_adapter.finalize().stdout
    over_value: object = load_object(over_bytes)
    for field in response_path:
        over_value = cast('dict[str, object]', over_value)[field]

    assert isinstance(exact_value, str)
    assert len(exact_value) == _CLAUDE_FIELD_LIMIT_CHARACTERS
    assert exact_value.endswith(exact_text)
    assert isinstance(over_value, str)
    assert len(over_value) == _CLAUDE_FIELD_LIMIT_CHARACTERS
    assert over_value.endswith('[truncated by sequential-hooks]')


@pytest.mark.parametrize(
    ('field_kind', 'event_fixture'),
    [
        ('additional_context', 'claude_pre.json'),
        ('decision_reason', 'claude_pre.json'),
        ('system_message', 'claude_post.json'),
    ],
)
def test_claude_human_field_shedding_marks_later_content_omission(
    field_kind: str,
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))
    first_step = _step(1, 'first')
    first_text = 'a' * (_CLAUDE_FIELD_LIMIT_CHARACTERS - len(labeled(first_step, '')) - 10)
    second_step = _step(2, 'second-label-must-remain-whole')
    if field_kind == 'additional_context':
        first_result = _pre_protocol('allow', additional_context=first_text)
        second_result = _pre_protocol('allow', additional_context='second contribution')
    elif field_kind == 'decision_reason':
        first_result = _pre_protocol('ask', reason=first_text)
        second_result = _pre_protocol('ask', reason='second contribution')
    else:
        first_result = _protocol_output({'systemMessage': first_text})
        second_result = _protocol_output({'systemMessage': 'second contribution'})
    adapter.consume(first_step, first_result)

    control = adapter.consume(second_step, second_result)

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    if field_kind == 'system_message':
        actual = response['systemMessage']
    else:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        field = (
            'additionalContext'
            if field_kind == 'additional_context'
            else 'permissionDecisionReason'
        )
        actual = hook[field]
    first_label = labeled(first_step, '').removesuffix('\n')
    second_label = labeled(second_step, '').removesuffix('\n')
    assert isinstance(actual, str)
    assert actual.startswith(f'{first_label}\n')
    assert f'\n\n{second_label}' in actual
    assert actual.endswith(TRUNCATION_MARKER)
    assert len(actual) == _CLAUDE_FIELD_LIMIT_CHARACTERS


def test_claude_human_field_shedding_preserves_labels_at_marker_boundary() -> None:
    first_step = _step(1, 'first')
    first_label = labeled(first_step, '').removesuffix('\n')
    second_label_wrapper = len('[step 2: ]')
    second_name_characters = (
        _CLAUDE_FIELD_LIMIT_CHARACTERS
        - len(first_label)
        - 2
        - second_label_wrapper
        - len(TRUNCATION_MARKER)
    )
    second_step = _step(2, 's' * second_name_characters)
    second_label = labeled(second_step, '').removesuffix('\n')
    expected = f'{first_label}\n\n{second_label}{TRUNCATION_MARKER}'
    assert len(expected) == _CLAUDE_FIELD_LIMIT_CHARACTERS
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    adapter.consume(first_step, _protocol_output({'systemMessage': 'f' * 100}))

    control = adapter.consume(
        second_step,
        _protocol_output({'systemMessage': 's' * 100}),
    )

    assert control == 'continue'
    assert load_object(adapter.finalize().stdout)['systemMessage'] == expected


@pytest.mark.parametrize(
    ('event_fixture', 'allow_on_failure', 'expected_stop'),
    [
        ('claude_pre.json', False, True),
        ('claude_pre.json', True, False),
        ('claude_post.json', False, False),
    ],
)
def test_claude_impossible_single_label_uses_minimal_failure_response(
    event_fixture: str,
    allow_on_failure: bool,
    expected_stop: bool,
) -> None:
    step_name = 'x' * 9_959
    assert len('[step 1: ]') + len(step_name) + len(TRUNCATION_MARKER) == 10_001
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    failing_step = _step(1, step_name)

    control = adapter.consume(
        failing_step,
        _protocol_output(_system_message_output('m' * 100)),
    )

    _assert_minimal_oversized_failure(
        adapter,
        control,
        stream,
        failing_step,
        expected_stop=expected_stop,
    )


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_infrastructure_outcomes_enter_native_failure_policy(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))
    failure = StepFailure(FailureKind.TIMEOUT, 'bounded timeout diagnostic')
    result = StepResult(
        None,
        b'ignored stdout',
        '',
        failure,
        ContainmentState.CONTAINED,
    )

    control = adapter.consume(_step(1, 'slow-hook'), result)

    if event_fixture == 'claude_pre.json':
        assert control == 'stop'
    else:
        assert control == 'continue'
        assert 'timeout' in cast('str', load_object(adapter.finalize().stdout)['systemMessage'])


def test_claude_invalid_utf8_is_a_blocking_malformed_pre_tool_result() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1, 'validator'), _output(b'\xff'))

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == 'deny'
    assert 'malformed' in cast('str', hook['permissionDecisionReason'])
    assert 'malformed' in stream.getvalue()


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_large_malformed_output_stays_bounded(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))
    stdout = b'{' + (b'x' * (FINAL_RESPONSE_LIMIT_BYTES - 1))

    control = adapter.consume(_step(1), _output(stdout))

    if event_fixture == 'claude_pre.json':
        assert control == 'stop'
        response_bytes = adapter.finalize().stdout
    else:
        assert control == 'continue'
        response_bytes = adapter.finalize().stdout
    assert b'malformed' in response_bytes
    assert stdout not in response_bytes
    assert len(response_bytes) < FINAL_RESPONSE_LIMIT_BYTES


def test_claude_late_plain_discard_diagnostic_contains_actual_output() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    adapter.consume(
        _step(1, 'structured'),
        _protocol_output({'systemMessage': 'survives'}),
    )

    control = adapter.consume(
        _step(2, 'late-plain'),
        _output(b'late discarded plain'),
    )

    assert control == 'continue'
    assert "'late discarded plain'" in stream.getvalue()
    assert b'late discarded plain' not in adapter.finalize().stdout


@pytest.mark.parametrize(
    'surrogate_kind',
    [
        'decision_reason',
        'stop_reason',
        'system_message',
        'terminal_sequence',
        'updated_value',
    ],
)
@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_lone_surrogate_structured_output_uses_malformed_policy(
    event_fixture: str,
    allow_on_failure: bool,
    surrogate_kind: str,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    surrogate = '\ud800'
    if surrogate_kind == 'decision_reason':
        result = (
            _pre_protocol('allow', reason=surrogate)
            if event_fixture == 'claude_pre.json'
            else _protocol_output({'decision': 'block', 'reason': surrogate})
        )
    elif surrogate_kind == 'stop_reason':
        result = _protocol_output({'continue': False, 'stopReason': surrogate})
    elif surrogate_kind == 'system_message':
        result = _protocol_output({'systemMessage': surrogate})
    elif surrogate_kind == 'terminal_sequence':
        result = _protocol_output({'terminalSequence': surrogate})
    elif event_fixture == 'claude_pre.json':
        result = _pre_protocol(
            'allow',
            updated_input={'nested': {'value': surrogate}},
        )
    else:
        result = _post_protocol(
            updated_output={'nested': {'value': surrogate}},
        )

    control = adapter.consume(_step(1, 'surrogate'), result)

    should_stop = event_fixture == 'claude_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response_bytes = adapter.finalize().stdout
    assert b'malformed' in response_bytes
    assert '\\ud800' not in response_bytes.decode()
    assert 'malformed' in stream.getvalue()
    assert adapter.step_input() == raw_input


def test_claude_malformed_output_after_plain_is_not_relayed() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    adapter.consume(_step(1), _output(b'plain'))

    control = adapter.consume(_step(2), _output(b'{"broken":'))

    assert control == 'stop'
    assert b'permissionDecision' in adapter.finalize().stdout
    assert b'plain' not in adapter.finalize().stdout


def test_claude_mcp_mutation_is_rejected_for_a_non_mcp_tool() -> None:
    raw_input = _fixture_bytes('claude_post.json')
    adapter = _make_adapter(raw_input)

    control = adapter.consume(
        _step(1),
        _post_protocol(updated_mcp_output={'stdout': 'wrong tool family'}),
    )

    assert control == 'continue'
    assert adapter.step_input() == raw_input
    assert 'unsupported' in cast(
        'str',
        load_object(adapter.finalize().stdout)['systemMessage'],
    )


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_mistyped_event_decision_is_unsupported(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))

    control = adapter.consume(_step(1), _protocol_output({'decision': None}))

    if event_fixture == 'claude_pre.json':
        assert control == 'stop'
        response_bytes = adapter.finalize().stdout
    else:
        assert control == 'continue'
        response_bytes = adapter.finalize().stdout
    assert b'unsupported' in response_bytes


def test_claude_multiple_plain_responses_drop_only_whole_later_contributions() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    first = 'a' * 600_000
    second = 'b' * 600_000
    adapter.consume(_step(1, 'first'), _output(first.encode()))

    control = adapter.consume(_step(2, 'second'), _output(second.encode()))

    assert control == 'continue'
    response = adapter.finalize().stdout
    assert response == encode_plain_response(labeled(_step(1, 'first'), first))
    assert b'b' not in response
    diagnostic = stream.getvalue()
    assert 'plain stdout omitted' in diagnostic
    assert 'final response limit' in diagnostic
    assert 'b' * 100 in diagnostic
    assert second not in diagnostic
    assert len(diagnostic) < 2_000


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_mutation_strings_are_exact_or_undeliverable(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    adapter = _make_adapter(raw_input, allow_on_failure=allow_on_failure)
    oversized_string = 'x' * (_CLAUDE_FIELD_LIMIT_CHARACTERS + 1)
    if event_fixture == 'claude_pre.json':
        result = _pre_protocol(
            'allow',
            updated_input={'nested': {'command': oversized_string}},
        )
    else:
        result = _post_protocol(
            updated_output={'nested': {'stdout': oversized_string}},
        )

    control = adapter.consume(_step(1, 'mutator'), result)

    if event_fixture == 'claude_pre.json':
        assert control == 'stop'
        response_bytes = adapter.finalize().stdout
        hook = cast(
            'dict[str, object]',
            load_object(response_bytes)['hookSpecificOutput'],
        )
        assert hook['permissionDecision'] == 'deny'
        assert 'undeliverable mutation' in cast(
            'str',
            hook['permissionDecisionReason'],
        )
    else:
        assert control == 'continue'
        response_bytes = adapter.finalize().stdout
        assert 'undeliverable mutation' in cast(
            'str',
            load_object(response_bytes)['systemMessage'],
        )
    assert oversized_string.encode() not in response_bytes
    assert adapter.step_input() == raw_input


def test_claude_native_block_drops_mutation_as_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    seeded = adapter.consume(
        _step(1, 'mutator'),
        _output(
            json.dumps(
                {
                    'hookSpecificOutput': {
                        'hookEventName': 'PostToolUse',
                        'updatedToolOutput': {'rewritten': True},
                    }
                },
                separators=(',', ':'),
            ).encode()
            + b'\n'
        ),
    )
    assert seeded == 'continue'
    fit_response = ClaudeAdapter.fit_response
    calls = {'count': 0}

    def failing_fit(
        owner: object = None,
        *,
        minimal_reports: bool = False,
    ) -> None:
        del owner
        calls['count'] += 1
        if calls['count'] <= 3:
            raise AdapterOutputError(OutputFailureKind.OVERSIZED, 'probe overflow')
        fit_response(adapter, minimal_reports=minimal_reports)

    monkeypatch.setattr(adapter, 'fit_response', failing_fit)

    control = adapter.consume(
        _step(2, 'blocker'),
        StepResult(2, b'', 'native block reason'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    assert response['decision'] == 'block'
    assert 'updatedToolOutput' not in adapter.finalize().stdout.decode()
    assert 'accepted mutation dropped to deliver the blocking response' in stream.getvalue()


@pytest.mark.parametrize(
    'stdout',
    [
        b'plain text',
        b'plain\rtext',
        b'[not protocol',
        b'"scalar"',
        b'["array"]',
    ],
)
def test_claude_non_object_leading_output_is_native_plain_text(stdout: bytes) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _output(stdout))

    assert control == 'continue'
    assert adapter.finalize().stdout == stdout + b'\n'


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_object_prefixed_invalid_utf8_uses_malformed_policy(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(_step(1, 'invalid-utf8'), _output(b'{\xff'))

    should_stop = event_fixture == 'claude_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response_bytes = adapter.finalize().stdout
    assert b'malformed' in response_bytes
    assert 'malformed' in stream.getvalue()


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
def test_claude_other_nonzero_exit_reports_and_continues(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes(event_fixture))

    control = adapter.consume(_step(1), _output(returncode=7))

    assert control == 'continue'
    assert 'status 7' in cast(
        'str',
        load_object(adapter.finalize().stdout)['systemMessage'],
    )


def test_claude_override_applies_later_outcome_after_malformed_failure() -> None:
    adapter = _make_adapter(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=True,
    )
    adapter.consume(_step(1, 'broken'), _output(b'{"broken":'))

    control = adapter.consume(_step(2), _output(b'later plain text'))

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    assert '[step 1: broken]' in cast('str', response['systemMessage'])


def test_claude_override_reports_malformed_pre_tool_output_and_continues() -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=True,
    )

    control = adapter.consume(_step(1, 'validator'), _output(b'\xff'))

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    assert 'malformed' in cast('str', response['systemMessage'])
    assert 'malformed' in stream.getvalue()


def test_claude_plain_discard_diagnostic_contains_one_bounded_excerpt() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    discarded = 'x' * 5_000
    adapter.consume(_step(1, 'plain'), _output(discarded.encode()))

    control = adapter.consume(
        _step(2, 'structured'),
        _protocol_output({'systemMessage': 'survives'}),
    )

    assert control == 'continue'
    diagnostic = stream.getvalue()
    assert diagnostic.count('plain stdout omitted') == 1
    assert 'x' * 100 in diagnostic
    assert discarded not in diagnostic
    assert len(diagnostic) < 2_000


def test_claude_post_native_block_survives_boundary_mutation() -> None:
    mutation = _native_block_boundary_mutation()
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    accepted = adapter.consume(
        _step(1, 'mutator'),
        _output(
            json.dumps(
                {
                    'hookSpecificOutput': {
                        'hookEventName': 'PostToolUse',
                        'updatedToolOutput': mutation,
                    }
                },
                separators=(',', ':'),
            ).encode()
            + b'\n'
        ),
    )
    assert accepted == 'continue'

    control = adapter.consume(
        _step(2, 'blocker'),
        StepResult(2, b'', 'native block reason'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    assert response['decision'] == 'block'
    assert 'native block reason' in cast('str', response['reason'])
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['updatedToolOutput'] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_claude_post_tool_applies_structured_output_after_malformed_failure() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    adapter.consume(_step(1, 'broken'), _output(b'{"broken":'))

    control = adapter.consume(
        _step(2),
        _protocol_output({'systemMessage': 'structured message'}),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    message = cast('str', response['systemMessage'])
    assert '[step 1: broken]' in message
    assert '[step 2: step-2]\nstructured message' in message


def test_claude_post_tool_block_is_terminal_with_ordered_reason() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))

    control = adapter.consume(
        _step(1, 'reviewer'),
        _protocol_output({'decision': 'block', 'reason': 'bad result'}),
    )

    assert control == 'stop'
    assert load_object(adapter.finalize().stdout) == {
        'decision': 'block',
        'reason': '[step 1: reviewer]\nbad result',
    }


def test_claude_post_tool_combines_native_feedback_and_context() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output(
            {
                'continue': False,
                'stopReason': 'stop chain',
                'decision': 'block',
                'reason': 'block feedback',
                'systemMessage': 'user message',
                'hookSpecificOutput': {
                    'hookEventName': 'PostToolUse',
                    'additionalContext': 'model context',
                },
            }
        ),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert response['continue'] is False
    assert response['stopReason'] == labeled(_step(1), 'stop chain')
    assert response['decision'] == 'block'
    assert response['reason'] == labeled(_step(1), 'block feedback')
    assert response['systemMessage'] == labeled(_step(1), 'user message')
    assert hook['additionalContext'] == labeled(_step(1), 'model context')


def test_claude_post_tool_dual_mutation_form_is_unsupported_and_atomic() -> None:
    raw_input = _fixture_bytes('claude_post.json')
    adapter = _make_adapter(raw_input)

    control = adapter.consume(
        _step(1),
        _post_protocol(
            updated_output={'stdout': 'one'},
            updated_mcp_output={'stdout': 'two'},
        ),
    )

    assert control == 'continue'
    assert adapter.step_input() == raw_input
    assert 'unsupported' in cast(
        'str',
        load_object(adapter.finalize().stdout)['systemMessage'],
    )


def test_claude_post_tool_mutation_is_sequential_and_canonical() -> None:
    raw_input = _mcp_post_input()
    adapter = _make_adapter(raw_input)
    first_replacement = {'stdout': 'first', 'nested': {'preserve': 1}}
    second_replacement = {'stdout': 'second', 'nested': {'preserve': 2}}

    adapter.consume(_step(1), _post_protocol(updated_output=first_replacement))
    first_input = load_object(adapter.step_input())
    adapter.consume(_step(2), _post_protocol(updated_mcp_output=second_replacement))
    second_input = load_object(adapter.step_input())

    assert first_input['tool_response'] == first_replacement
    assert second_input['tool_response'] == second_replacement
    assert second_input['unknown_top_level'] == {'preserve': True}
    assert second_input['tool_input'] == {
        'command': "printf '%s\\n' hello",
        'description': 'Print a greeting',
        'unknown_nested': {'preserve': True},
    }
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['updatedToolOutput'] == second_replacement
    assert 'updatedMCPToolOutput' not in hook


def test_claude_pre_tool_mutation_is_sequential_and_preserves_unknown_input() -> None:
    raw_input = _fixture_bytes('claude_pre.json')
    adapter = _make_adapter(raw_input)
    first_replacement = {'command': 'printf first', 'new_nested': {'x': 1}}
    second_replacement = {'command': 'printf second', 'new_nested': {'x': 2}}

    assert adapter.step_input() == raw_input
    adapter.consume(_step(1), _pre_protocol('allow', updated_input=first_replacement))
    first_input = load_object(adapter.step_input())
    adapter.consume(_step(2), _pre_protocol('ask', updated_input=second_replacement))
    second_input = load_object(adapter.step_input())

    assert first_input['tool_input'] == first_replacement
    assert second_input['tool_input'] == second_replacement
    assert first_input['unknown_top_level'] == {'preserve': True}
    assert second_input['unknown_top_level'] == {'preserve': True}
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['updatedInput'] == second_replacement


def test_claude_pre_tool_reasons_survive_only_at_winning_rank() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    adapter.consume(_step(1), _pre_protocol('allow', reason='low'))
    adapter.consume(_step(2), _pre_protocol('ask', reason='high one'))
    adapter.consume(_step(3), _pre_protocol('ask', reason='high two'))
    adapter.consume(_step(4), _pre_protocol('allow', reason='late low'))

    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )

    assert hook['permissionDecisionReason'] == (
        '[step 2: step-2]\nhigh one\n\n[step 3: step-3]\nhigh two'
    )


@pytest.mark.parametrize('first', _CLAUDE_PRE_RANK)
@pytest.mark.parametrize('second', _CLAUDE_PRE_RANK)
def test_claude_pre_tool_uses_documented_decision_rank(
    first: str,
    second: str,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    controls: list[ChainControl] = []
    for index, decision in enumerate((first, second), 1):
        result = _output() if decision == 'neutral' else _pre_protocol(decision)
        control = adapter.consume(_step(index), result)
        controls.append(control)
        if control == 'stop':
            break

    expected = max((first, second), key=lambda decision: _CLAUDE_PRE_RANK[decision])
    if expected == 'neutral':
        assert adapter.finalize().stdout == b''
    else:
        response = load_object(adapter.finalize().stdout)
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == expected
    assert (controls[-1] == 'stop') is (first == 'deny' or second == 'deny')


def test_claude_rejected_candidate_discards_tentative_terminal_diagnostic() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('claude_post.json'))
    first_terminal = 't' * 1_000
    adapter.consume(
        _step(1, 'first-terminal'),
        _protocol_output({'terminalSequence': first_terminal}),
    )
    mutation = _near_budget_mutation()

    control = adapter.consume(
        _step(2, 'rejected'),
        _protocol_output(
            {
                'terminalSequence': 'duplicate',
                'hookSpecificOutput': {
                    'hookEventName': 'PostToolUse',
                    'updatedToolOutput': mutation,
                },
            }
        ),
    )

    assert control == 'continue'
    assert 'earlier step supplied one' not in stream.getvalue()
    assert 'undeliverable mutation' in stream.getvalue()
    response = load_object(adapter.finalize().stdout)
    assert response['terminalSequence'] == first_terminal
    assert 'hookSpecificOutput' not in response


def test_claude_rejected_structured_candidate_emits_plain_discard_once() -> None:
    raw_input = (
        b'{"hook_event_name":"PostToolUse","tool_name":"Bash",'
        b'"tool_input":{},"tool_response":{},"unknown":"\\ud800"}\n'
    )
    adapter, stream = _make_adapter_with_stream(raw_input)
    adapter.consume(_step(1, 'plain'), _output(b'actual discarded plain'))

    control = adapter.consume(
        _step(2, 'rejected'),
        _post_protocol(
            updated_output={'stdout': 'replacement'},
        ),
    )

    assert control == 'continue'
    diagnostic = stream.getvalue()
    assert diagnostic.count('plain stdout omitted') == 1
    assert diagnostic.count("'actual discarded plain'") == 1
    assert 'malformed' in diagnostic
    assert adapter.step_input() == raw_input


@pytest.mark.parametrize(
    'raw_input',
    [
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":null}',
        (
            b'{"hook_event_name":"PostToolUse","tool_name":"Bash",'
            b'"tool_input":false,"tool_response":[1,"two",null]}'
        ),
    ],
)
def test_claude_required_payload_fields_accept_any_strict_json_value(
    raw_input: bytes,
) -> None:
    adapter = _make_adapter(raw_input)

    assert adapter.step_input() == raw_input


def test_claude_single_plain_response_exact_final_budget_boundary() -> None:
    exact_adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    exact_text = b'x' * (FINAL_RESPONSE_LIMIT_BYTES - 1)
    exact_control = exact_adapter.consume(_step(1), _output(exact_text))

    oversized_adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    oversized_control = oversized_adapter.consume(
        _step(1),
        _output(b'x' * FINAL_RESPONSE_LIMIT_BYTES),
    )

    assert exact_control == 'continue'
    assert exact_adapter.finalize().stdout == exact_text + b'\n'
    assert oversized_control == 'continue'
    oversized_response = load_object(oversized_adapter.finalize().stdout)
    assert 'oversized' in cast('str', oversized_response['systemMessage'])


def test_claude_stop_reason_without_continue_false_is_unsupported() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output({'stopReason': 'orphan reason'}),
    )

    assert control == 'stop'
    assert b'unsupported' in adapter.finalize().stdout


@pytest.mark.parametrize('value', [True, False])
def test_claude_suppress_output_is_sticky_when_true(value: bool) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    adapter.consume(_step(1), _protocol_output({'suppressOutput': value}))
    adapter.consume(_step(2), _protocol_output({'suppressOutput': False}))

    response = load_object(adapter.finalize().stdout)

    assert response.get('suppressOutput') is value


def test_claude_system_messages_are_labeled_in_step_order() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_post.json'))
    adapter.consume(_step(1, 'first'), _protocol_output({'systemMessage': 'one'}))
    adapter.consume(_step(2, 'second'), _protocol_output({'systemMessage': 'two'}))

    response = load_object(adapter.finalize().stdout)

    assert response['systemMessage'] == ('[step 1: first]\none\n\n[step 2: second]\ntwo')


def test_claude_terminal_decision_prevents_later_malformed_step() -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    executor = _QueueExecutor(
        [
            _pre_protocol('deny', reason='terminal'),
            _output(b'{"must_not_run":'),
        ]
    )
    steps = (_step(1, 'blocker'), _step(2, 'candidate'))

    response = run_pipeline(
        adapter,
        steps,
        executor,
        None,
        allow_on_failure=False,
    )

    assert executor.calls == [steps[0]]
    hook = cast(
        'dict[str, object]',
        load_object(response.stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecision'] == 'deny'


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_terminal_overflow_after_prior_mutation_blocks_pipeline(
    allow_on_failure: bool,
) -> None:
    mutation = _terminal_boundary_mutation()
    adapter = _make_adapter(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=allow_on_failure,
    )
    executor = _QueueExecutor(
        [
            _pre_protocol('allow', updated_input=mutation),
            _protocol_output({'continue': False}),
            _output(b'must not run'),
        ]
    )
    steps = (
        _step(1, 'mutator'),
        _step(2, 'terminal'),
        _step(3, 'must-not-run'),
    )

    response = run_pipeline(
        adapter,
        steps,
        executor,
        None,
        allow_on_failure=allow_on_failure,
    )

    assert executor.calls == list(steps[:2])
    hook = cast(
        'dict[str, object]',
        load_object(response.stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecision'] == 'deny'
    assert 'undeliverable mutation' in cast(
        'str',
        hook['permissionDecisionReason'],
    )
    assert load_object(adapter.step_input())['tool_input'] == mutation


def test_claude_terminal_sequence_is_exact_or_whole_contribution_oversized() -> None:
    exact = 'x' * _CLAUDE_FIELD_LIMIT_CHARACTERS
    exact_adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    exact_adapter.consume(_step(1), _protocol_output({'terminalSequence': exact}))

    over_adapter = _make_adapter(_fixture_bytes('claude_pre.json'))
    over_control = over_adapter.consume(
        _step(1),
        _protocol_output({'terminalSequence': f'{exact}x'}),
    )

    assert load_object(exact_adapter.finalize().stdout)['terminalSequence'] == exact
    assert over_control == 'stop'
    over_response = load_object(over_adapter.finalize().stdout)
    assert 'terminalSequence' not in over_response
    hook = cast('dict[str, object]', over_response['hookSpecificOutput'])
    assert 'oversized' in cast('str', hook['permissionDecisionReason'])


def test_claude_uncontained_termination_warns_once_and_keeps_categories() -> None:
    adapter = _make_adapter(
        _fixture_bytes('claude_pre.json'),
        allow_on_failure=True,
    )
    for index, kind in enumerate(
        (FailureKind.TIMEOUT, FailureKind.OUTPUT_LIMIT),
        1,
    ):
        adapter.consume(
            _step(index),
            StepResult(
                None,
                b'',
                '',
                StepFailure(kind, f'{kind.value} detail'),
                ContainmentState.UNCONTAINED,
            ),
        )

    message = cast(
        'str',
        load_object(adapter.finalize().stdout)['systemMessage'],
    )

    assert message.count('without containment') == 1
    assert 'timeout' in message
    assert 'output limit' in message


@pytest.mark.parametrize(
    'value',
    [
        {'unknown': True},
        {'continue': 'false'},
        {'suppressOutput': 1},
        {'systemMessage': 1},
        {'terminalSequence': 1},
    ],
)
def test_claude_unknown_or_mistyped_universal_field_is_unsupported(
    value: JsonObject,
) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _protocol_output(value))

    assert control == 'stop'
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert 'unsupported' in cast('str', hook['permissionDecisionReason'])


@pytest.mark.parametrize(
    'stdout',
    [
        b' ',
        b'\t',
        b'\r',
        b'\n',
        b' \t\r\n\t ',
    ],
)
def test_claude_whitespace_only_stdout_is_neutral(stdout: bytes) -> None:
    adapter = _make_adapter(_fixture_bytes('claude_pre.json'))

    control = adapter.consume(_step(1), _output(stdout))

    assert control == 'continue'
    assert adapter.finalize().stdout == b''


@pytest.mark.parametrize('event_fixture', ['claude_pre.json', 'claude_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_claude_final_fit_preserves_reasonless_terminal_after_prior_mutation(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    mutation = _tight_budget_mutation()
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    initial = (
        _pre_protocol('allow', updated_input=mutation)
        if event_fixture == 'claude_pre.json'
        else _post_protocol(updated_output=mutation)
    )
    adapter.consume(_step(1, 'mutator'), initial)

    control = adapter.consume(
        _step(2, 'terminal'),
        _protocol_output({'continue': False}),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    assert response['continue'] is False
    assert 'stopReason' not in response
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    mutation_field = 'updatedInput' if event_fixture == 'claude_pre.json' else 'updatedToolOutput'
    assert hook[mutation_field] == mutation
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_configuration_response_is_independent_of_adapter_construction(
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('configuration response must not construct an adapter')

    monkeypatch.setattr('plugins.claude._sequential_hooks._adapter.ClaudeAdapter', unavailable)
    monkeypatch.setattr(ClaudeHost, 'adapter', unavailable)
    stream = io.StringIO()
    response = ClaudeHost().configuration_response(
        event, 'invalid configuration', Diagnostics(stream)
    )
    message = '[step 1: configuration]\nspawn: sequential-hooks could not safely run this hook'
    expected: JsonObject = {'systemMessage': message}
    if event == 'PreToolUse':
        expected['hookSpecificOutput'] = {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': message,
        }
    else:
        expected.update(decision='block', reason=message)

    assert response.exit_code == 0
    assert json.loads(response.stdout) == expected
    assert response.stdout.endswith(b'\n')
    assert response.stdout.count(b'\n') == 1


def test_host_contract_metadata() -> None:
    host = ClaudeHost()

    assert type(host) is ClaudeHost
    assert (host.api_version, host.name, host.events, host.requires_event) == (
        API_VERSION,
        'claude',
        ('PreToolUse', 'PostToolUse'),
        False,
    )
