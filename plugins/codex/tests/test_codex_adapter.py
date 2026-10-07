# pyright: reportPrivateUsage=false

import io
import json
from pathlib import Path
from typing import cast

import pytest

import sequential_hooks.hosts._skeleton as skeleton_module
from plugins.codex._sequential_hooks._adapter import CodexHost, _CodexAdapter
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
    labeled,
)
from sequential_hooks.hosts._json import load_object

_BOUNDARY_CANDIDATE_REASON = 'CANDIDATE_ONLY'
_BOUNDARY_PRIOR_COUNT = 1
_BOUNDARY_PRIOR_REASON = 'p' * 100
_EXIT_TWO_SENTINEL = 'SPECIFIC_CHILD_REASON_7fca'
_FIXTURE_ROOT = Path(__file__).parent / 'fixtures'
_IMPOSSIBLE_STEP_NAME = 'x' * FINAL_RESPONSE_LIMIT_BYTES


class _OutputFailureAdapter(_CodexAdapter):
    def consume_output_failure(
        self,
        step: Step,
        failure: OutputFailureKind,
    ) -> ChainControl:
        return self._apply_failure(step, failure.value, f'{failure.value} detail')


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


def _chunked_mutation(character_count: int) -> JsonObject:
    chunks: list[JsonValue] = []
    remaining = character_count
    while remaining:
        size = min(9_000, remaining)
        chunks.append('m' * size)
        remaining -= size
    return {'chunks': chunks}


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURE_ROOT / name).read_bytes()


def _input_for_tool(tool_name: str) -> bytes:
    payload = load_object(_fixture_bytes('codex_pre.json'))
    payload['tool_name'] = tool_name
    return f'{json.dumps(payload, separators=(",", ":"))}\n'.encode()


def _make_adapter_with_stream(
    raw_input: bytes,
    *,
    allow_on_failure: bool = False,
) -> tuple[_CodexAdapter, io.StringIO]:
    payload = load_object(raw_input)
    stream = io.StringIO()
    adapter = _CodexAdapter(
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
) -> _CodexAdapter:
    adapter, _stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    return adapter


def _output(
    stdout: bytes = b'',
    *,
    returncode: int = 0,
    blocking_stderr: str = '',
) -> StepResult:
    return StepResult(returncode, stdout, blocking_stderr)


def _protocol_output(value: JsonObject) -> StepResult:
    return _output(f'{json.dumps(value, separators=(",", ":"))}\n'.encode())


def _pre_protocol(
    decision: str | None = None,
    *,
    additional_context: str | None = None,
    reason: str | None = None,
    updated_input: object = ...,
) -> StepResult:
    hook: JsonObject = {'hookEventName': 'PreToolUse'}
    if decision is not None:
        hook['permissionDecision'] = decision
    if additional_context is not None:
        hook['additionalContext'] = additional_context
    if reason is not None:
        hook['permissionDecisionReason'] = reason
    if updated_input is not ...:
        hook['updatedInput'] = cast('JsonValue', updated_input)
    return _protocol_output({'hookSpecificOutput': hook})


def _response_bytes(adapter: _CodexAdapter) -> bytes:
    return adapter.finalize().stdout


def _response_message(adapter: _CodexAdapter) -> str:
    response = load_object(_response_bytes(adapter))
    if 'systemMessage' in response:
        return cast('str', response['systemMessage'])
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    return cast('str', hook['permissionDecisionReason'])


def _step(index: int, name: str | None = None) -> Step:
    return Step(index, (name or f'step-{index}',))


def _boundary_mutation(extra_bytes: int = 0) -> tuple[JsonObject, bytes]:
    labels = [
        labeled(_step(index, f'prior-{index}'), '').removesuffix('\n')
        for index in range(1, _BOUNDARY_PRIOR_COUNT + 1)
    ]
    labels.append(
        labeled(
            _step(_BOUNDARY_PRIOR_COUNT + 1, 'candidate'),
            '',
        ).removesuffix('\n')
    )
    joined_labels = '\n\n'.join(labels)
    minimum_reason = f'{joined_labels}{TRUNCATION_MARKER}'
    mutation: JsonObject = {
        'command': 'safe',
        'padding': '',
    }
    response: JsonObject = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'permissionDecisionReason': minimum_reason,
            'updatedInput': mutation,
        }
    }
    base = f'{json.dumps(response, separators=(",", ":"))}\n'.encode()
    padding = FINAL_RESPONSE_LIMIT_BYTES - len(base) + extra_bytes
    assert padding >= 0
    mutation['padding'] = 'm' * padding
    expected = f'{json.dumps(response, separators=(",", ":"))}\n'.encode()
    assert len(expected) == FINAL_RESPONSE_LIMIT_BYTES + extra_bytes
    return mutation, expected


def _accept_exact_boundary_mutation(adapter: _CodexAdapter) -> JsonObject:
    for index in range(1, _BOUNDARY_PRIOR_COUNT + 1):
        adapter.consume(
            _step(index, f'prior-{index}'),
            _pre_protocol(
                'allow',
                reason=_BOUNDARY_PRIOR_REASON,
                updated_input={'command': f'prior-{index}'},
            ),
        )
    mutation, expected = _boundary_mutation()
    candidate = _pre_protocol(
        'allow',
        reason=_BOUNDARY_CANDIDATE_REASON,
        updated_input=mutation,
    )
    assert len(candidate.stdout) < _STDOUT_LIMIT_BYTES
    control = adapter.consume(
        _step(_BOUNDARY_PRIOR_COUNT + 1, 'candidate'),
        candidate,
    )
    assert control == 'continue'
    assert adapter.finalize().stdout == expected
    return mutation


@pytest.mark.parametrize(
    ('event_fixture', 'allow_on_failure'),
    [
        ('codex_pre.json', False),
        ('codex_pre.json', True),
        ('codex_post.json', False),
        ('codex_post.json', True),
    ],
)
def test_codex_accumulated_attribution_keeps_current_failure_report(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    prior = _protocol_output(
        {'systemMessage': 'm' * (FINAL_RESPONSE_LIMIT_BYTES - 500)},
    )
    assert len(prior.stdout) < _STDOUT_LIMIT_BYTES
    assert adapter.consume(_step(1, 'prior'), prior) == 'continue'

    control = adapter.consume(
        _step(2, 'failed'),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
        ),
    )

    should_stop = event_fixture == 'codex_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response = load_object(_response_bytes(adapter))
    assert 'timeout' in cast('str', response['systemMessage'])
    if should_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'


@pytest.mark.parametrize(
    ('fixture_name', 'event'),
    [
        ('codex_pre.json', 'PreToolUse'),
        ('codex_post.json', 'PostToolUse'),
    ],
)
def test_codex_adapter_accepts_realistic_strict_input(
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
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash"}',
        b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{}}',
        b'{"hook_event_name":1,"tool_name":"Bash","tool_input":{}}',
        b'{"hook_event_name":"PreToolUse","tool_name":"","tool_input":{}}',
        b'{"hook_event_name":"PreToolUse","tool_name":1,"tool_input":{}}',
        b'{"hook_event_name":"Other","tool_name":"Bash","tool_input":{}}',
    ],
)
def test_codex_adapter_rejects_invalid_required_input(raw_input: bytes) -> None:
    with pytest.raises(ValueError, match='Codex'):
        _make_adapter(raw_input)


def test_codex_current_and_legacy_pre_decisions_are_ambiguous() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output(
            {
                'decision': 'block',
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'deny',
                },
            }
        ),
    )

    assert control == 'stop'
    assert 'unsupported' in _response_message(adapter)


@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('depth', [300, 600])
def test_codex_deep_mutation_uses_normal_failure_policy(
    allow_on_failure: bool,
    depth: int,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=allow_on_failure,
    )
    prior_input = adapter.step_input()
    hook = (
        b'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
        b'"permissionDecision":"allow","updatedInput":'
    )
    mutation = (
        b'{"command":"replacement","nested":' + b'{"next":' * depth + b'null' + b'}' * depth + b'}'
    )
    stdout = hook + mutation + b'}}'
    # The actual strict loader accepts both depths; rejection belongs to composition.
    load_object(stdout)
    steps = (_step(1, 'mutator'), _step(2, 'later'))
    executor = _QueueExecutor([_output(stdout), _protocol_output({'systemMessage': 'later ran'})])

    response = run_pipeline(
        adapter,
        steps,
        executor,
        None,
        allow_on_failure=allow_on_failure,
    )

    rejected = depth == 600
    stopped = rejected and not allow_on_failure
    assert executor.calls == list(steps[:1] if stopped else steps)
    assert response.exit_code == 0
    assert (b'later ran' in response.stdout) is not stopped
    if rejected:
        assert adapter.step_input() == prior_input
        assert 'malformed' in stream.getvalue()
        assert b'malformed' in response.stdout
        if stopped:
            assert b'"permissionDecision":"deny"' in response.stdout
    else:
        assert adapter.step_input() != prior_input
        assert not stream.getvalue()


def test_codex_developer_text_diagnostic_is_bounded() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('codex_post.json'))
    text = b'x' * 20_000

    control = adapter.consume(_step(1, 'developer'), _output(text))

    assert control == 'continue'
    assert adapter.finalize().stdout == b''
    diagnostic = stream.getvalue()
    assert 'x' * 100 in diagnostic
    assert text.decode() not in diagnostic
    assert len(diagnostic) < 2_000


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_codex_exact_boundary_mutation_cannot_relax_later_terminal_deny(
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=allow_on_failure,
    )
    mutation = _accept_exact_boundary_mutation(adapter)

    control = adapter.consume(
        _step(_BOUNDARY_PRIOR_COUNT + 2, _IMPOSSIBLE_STEP_NAME),
        _pre_protocol('deny', reason='terminal deny'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == 'deny'
    assert 'updatedInput' not in hook
    assert load_object(adapter.step_input())['tool_input'] == mutation
    assert _IMPOSSIBLE_STEP_NAME.encode() not in adapter.finalize().stdout


def test_codex_exact_budget_report_raises_instead_of_committing_deny() -> None:
    adapter = _make_adapter(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=True,
    )
    mutation = _accept_exact_boundary_mutation(adapter)
    prior_input = adapter.step_input()

    with pytest.raises(AdapterOutputError) as raised:
        adapter.consume(
            _step(_BOUNDARY_PRIOR_COUNT + 2, 'FIRST_BOUNDARY_EXIT_41'),
            _output(returncode=41),
        )

    assert raised.value.kind is OutputFailureKind.OVERSIZED
    assert adapter.step_input() == prior_input
    assert load_object(prior_input)['tool_input'] == mutation


@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_exit_two_is_terminal_native_feedback_without_wrapper_warning(
    event_fixture: str,
) -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes(event_fixture))

    control = adapter.consume(
        _step(1, 'blocker'),
        _output(returncode=2, blocking_stderr='bounded native feedback'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    if event_fixture == 'codex_pre.json':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert 'bounded native feedback' in cast(
            'str',
            hook['permissionDecisionReason'],
        )
    else:
        assert response['decision'] == 'block'
        assert 'bounded native feedback' in cast('str', response['reason'])
    assert 'systemMessage' not in response
    assert stream.getvalue() == ''


def test_codex_failed_mutation_publication_restores_exact_prior_state(
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
                'additionalContext': 'prior context',
            },
        }
    )
    assert adapter.consume(_step(1, 'prior'), prior_result) == 'continue'
    prior_state = vars(adapter.state)
    prior_payload = prior_state['payload']
    prior_raw_input = adapter.step_input()
    prior_contexts = list(cast('list[str]', prior_state['contexts']))
    prior_messages = list(cast('list[object]', prior_state['messages']))
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


def test_codex_failure_override_reports_bounded_diagnostic_and_concise_message() -> None:
    detail = 'x' * 20_000
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=True,
    )

    control = adapter.consume(
        _step(1, 'failed'),
        StepResult(None, b'', '', StepFailure(FailureKind.TIMEOUT, detail)),
    )

    assert control == 'continue'
    message = _response_message(adapter)
    assert 'timeout' in message
    assert 'developer diagnostics' in message
    assert detail not in message
    diagnostic = stream.getvalue()
    assert 'x' * 100 in diagnostic
    assert detail not in diagnostic
    assert len(diagnostic) < 2_000


def test_codex_final_budget_sheds_context_before_system_message() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    mutation = {
        'command': 'safe',
        **_chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500),
    }
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


def test_codex_fitting_borrows_accepted_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    mutation = {
        'command': 'safe',
        **_chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500),
    }
    assert (
        adapter.consume(
            _step(1),
            _pre_protocol(
                'allow',
                additional_context='c' * 6_000,
                updated_input=mutation,
            ),
        )
    ) == 'continue'
    next_input = adapter.step_input()
    owned_mutation = vars(adapter.state)['updated_input']
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
    assert mutation_copy_count == 0
    assert adapter.step_input() == next_input
    assert load_object(response_bytes) == {
        'systemMessage': labeled(_step(2), 'm' * 3_000),
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
            'updatedInput': mutation,
        },
    }
    assert len(response_bytes) <= FINAL_RESPONSE_LIMIT_BYTES


def test_codex_fitting_builds_and_encodes_each_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    mutation = {
        'command': 'safe',
        **_chunked_mutation(FINAL_RESPONSE_LIMIT_BYTES - 9_500),
    }
    adapter.consume(
        _step(1),
        _pre_protocol(
            'allow',
            additional_context='c' * 6_000,
            updated_input=mutation,
        ),
    )
    counts = {'build': 0, 'encode': 0}
    final_object = _CodexAdapter.build_response
    encode_response = skeleton_module.encode_response

    def counted_final_object(self: _CodexAdapter) -> JsonObject:
        counts['build'] += 1
        return final_object(self)

    def counted_encode_response(response: JsonObject) -> bytes:
        counts['encode'] += 1
        return encode_response(response)

    monkeypatch.setattr(_CodexAdapter, 'build_response', counted_final_object)
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


@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_impossible_attribution_exit_two_stays_terminal(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(
        _step(1, _IMPOSSIBLE_STEP_NAME),
        _output(returncode=2, blocking_stderr=_EXIT_TWO_SENTINEL),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    if event_fixture == 'codex_pre.json':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert hook['permissionDecisionReason'] == _EXIT_TWO_SENTINEL
    else:
        assert response['decision'] == 'block'
        assert response['reason'] == _EXIT_TWO_SENTINEL
    assert _IMPOSSIBLE_STEP_NAME.encode() not in adapter.finalize().stdout


@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize(
    ('event_fixture', 'value', 'control_field'),
    [
        (
            'codex_pre.json',
            {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'deny',
                    'permissionDecisionReason': 'native deny',
                }
            },
            'permissionDecision',
        ),
        (
            'codex_post.json',
            {'decision': 'block', 'reason': 'native block'},
            'decision',
        ),
        (
            'codex_post.json',
            {'continue': False, 'stopReason': 'native stop'},
            'continue',
        ),
    ],
)
def test_codex_impossible_attribution_keeps_native_terminal_control(
    event_fixture: str,
    value: JsonObject,
    control_field: str,
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(
        _step(1, _IMPOSSIBLE_STEP_NAME),
        _protocol_output(value),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    if control_field == 'permissionDecision':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert 'ask' not in adapter.finalize().stdout.decode()
    elif control_field == 'decision':
        assert response['decision'] == 'block'
    else:
        assert response['continue'] is False
    assert _IMPOSSIBLE_STEP_NAME.encode() not in adapter.finalize().stdout
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


@pytest.mark.parametrize(
    ('report_class', 'event_fixture', 'allow_on_failure'),
    [
        ('failure', 'codex_pre.json', True),
        ('failure', 'codex_post.json', False),
        ('failure', 'codex_post.json', True),
        ('uncontained', 'codex_pre.json', True),
        ('uncontained', 'codex_post.json', False),
        ('uncontained', 'codex_post.json', True),
        ('ordinary_exit', 'codex_pre.json', False),
        ('ordinary_exit', 'codex_pre.json', True),
        ('ordinary_exit', 'codex_post.json', False),
        ('ordinary_exit', 'codex_post.json', True),
    ],
)
def test_codex_impossible_attribution_terminal_retains_each_mandatory_report(
    report_class: str,
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    if report_class == 'ordinary_exit':
        result = _output(returncode=7)
        expected_reports = ('exited with status 7',)
    else:
        containment = (
            ContainmentState.UNCONTAINED
            if report_class == 'uncontained'
            else ContainmentState.CONTAINED
        )
        result = StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
            containment,
        )
        expected_reports = (
            ('without containment', 'timeout') if report_class == 'uncontained' else ('timeout',)
        )
    prior = adapter.consume(
        _step(1, report_class),
        result,
    )
    assert prior == 'continue'

    control = adapter.consume(
        _step(2, _IMPOSSIBLE_STEP_NAME),
        _output(returncode=2, blocking_stderr=_EXIT_TWO_SENTINEL),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    message = cast('str', response['systemMessage'])
    for expected_report in expected_reports:
        assert expected_report in message
    if report_class == 'uncontained':
        assert message.count('without containment') == 1
    if event_fixture == 'codex_pre.json':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert hook['permissionDecisionReason'] == _EXIT_TWO_SENTINEL
    else:
        assert response['decision'] == 'block'
        assert response['reason'] == _EXIT_TWO_SENTINEL
    assert _IMPOSSIBLE_STEP_NAME.encode() not in adapter.finalize().stdout
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


def test_codex_impossible_attribution_uncontained_warning_is_bounded_once() -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=True,
    )

    for failure_kind in (FailureKind.TIMEOUT, FailureKind.OUTPUT_LIMIT):
        control = adapter.consume(
            _step(1, _IMPOSSIBLE_STEP_NAME),
            StepResult(
                None,
                b'',
                '',
                StepFailure(failure_kind, f'{failure_kind.value} detail'),
                ContainmentState.UNCONTAINED,
            ),
        )
        assert control == 'continue'

    response = load_object(adapter.finalize().stdout)
    message = cast('str', response['systemMessage'])
    assert message.count('without containment') == 1
    assert 'timeout' in message
    assert 'output limit' in message
    diagnostic_lines = stream.getvalue().splitlines()
    assert len(diagnostic_lines) == 3
    assert sum('without containment' in line for line in diagnostic_lines) == 1
    assert all(len(line) <= 1_000 for line in diagnostic_lines)


def test_codex_impossible_attribution_uncontained_warning_survives_stop() -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('codex_pre.json'))

    control = adapter.consume(
        _step(1, _IMPOSSIBLE_STEP_NAME),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
            ContainmentState.UNCONTAINED,
        ),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    message = cast('str', response['systemMessage'])
    assert message.count('without containment') == 1
    assert 'timeout' in message
    diagnostic_lines = stream.getvalue().splitlines()
    assert len(diagnostic_lines) == 2
    assert sum('without containment' in line for line in diagnostic_lines) == 1
    assert all(len(line) <= 1_000 for line in diagnostic_lines)


@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_impossible_attribution_wrapper_failure_keeps_bounded_reports(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(
        _step(1, _IMPOSSIBLE_STEP_NAME),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'd' * FINAL_RESPONSE_LIMIT_BYTES),
        ),
    )

    should_stop = event_fixture == 'codex_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response = load_object(_response_bytes(adapter))
    assert 'timeout' in cast('str', response['systemMessage'])
    if should_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert 'timeout' in cast('str', hook['permissionDecisionReason'])
    assert _IMPOSSIBLE_STEP_NAME.encode() not in _response_bytes(adapter)
    diagnostic_lines = stream.getvalue().splitlines()
    assert len(diagnostic_lines) == 1
    assert 'timeout' in diagnostic_lines[0]
    assert len(diagnostic_lines[0]) <= 1_000


@pytest.mark.parametrize('failure_kind', list(FailureKind))
@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_infrastructure_failure_uses_common_matrix(
    event_fixture: str,
    allow_on_failure: bool,
    failure_kind: FailureKind,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    result = StepResult(
        None,
        b'ignored',
        '',
        StepFailure(failure_kind, f'{failure_kind.value} detail'),
        ContainmentState.CONTAINED,
    )

    control = adapter.consume(_step(1, 'failed'), result)

    should_stop = event_fixture == 'codex_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response = load_object(_response_bytes(adapter))
    category = failure_kind.value.replace('_', ' ')
    assert category in cast('str', response['systemMessage'])
    if should_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert category in cast('str', hook['permissionDecisionReason'])
    diagnostic = stream.getvalue()
    assert category in diagnostic
    assert diagnostic.count('sequential-hooks: step 1 (failed):') == 1


def test_codex_later_terminal_deny_omits_prior_replacement_without_undoing_input() -> None:
    mutation = {'command': 'safe'}
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    adapter.consume(
        _step(1, 'mutator'),
        _pre_protocol('allow', updated_input=mutation),
    )

    control = adapter.consume(
        _step(2, 'blocker'),
        _pre_protocol('deny', reason='blocked'),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == 'deny'
    assert 'updatedInput' not in hook
    assert load_object(adapter.step_input())['tool_input'] == mutation


def test_codex_legacy_pre_block_normalizes_to_current_shape() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))

    control = adapter.consume(
        _step(1, 'legacy'),
        _protocol_output({'decision': 'block', 'reason': 'legacy block'}),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    assert 'decision' not in response
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook == {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': '[step 1: legacy]\nlegacy block',
    }


@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_codex_lone_surrogate_output_uses_malformed_policy(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(
        _step(1, 'surrogate'),
        _protocol_output({'systemMessage': '\ud800'}),
    )

    should_stop = event_fixture == 'codex_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    final = _response_bytes(adapter)
    assert b'malformed' in final
    assert '\\ud800' not in final.decode()
    assert 'malformed' in stream.getvalue()
    assert adapter.step_input() == raw_input


@pytest.mark.parametrize('report_class', ['failure', 'uncontained', 'ordinary_exit'])
def test_codex_mandatory_report_raises_when_allow_cannot_fit(report_class: str) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=True,
    )
    mutation = _accept_exact_boundary_mutation(adapter)
    if report_class == 'ordinary_exit':
        result = _output(returncode=7)
    else:
        containment = (
            ContainmentState.UNCONTAINED
            if report_class == 'uncontained'
            else ContainmentState.CONTAINED
        )
        result = StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
            containment,
        )

    with pytest.raises(AdapterOutputError) as raised:
        adapter.consume(_step(_BOUNDARY_PRIOR_COUNT + 2, report_class), result)

    assert raised.value.kind is OutputFailureKind.OVERSIZED
    assert load_object(adapter.step_input())['tool_input'] == mutation
    diagnostic = stream.getvalue()
    if report_class == 'ordinary_exit':
        assert 'exited with status 7' in diagnostic
    else:
        assert 'timeout: ' in diagnostic
    if report_class == 'uncontained':
        assert 'without containment' in diagnostic


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_codex_mutation_serialization_failure_restores_complete_state(
    allow_on_failure: bool,
) -> None:
    raw_input = (
        b'{"hook_event_name":"PreToolUse","tool_name":"Bash",'
        b'"tool_input":{"command":"original"},"unknown":"\\ud800"}\n'
    )
    original_payload = load_object(raw_input)
    adapter, stream = _make_adapter_with_stream(
        raw_input,
        allow_on_failure=allow_on_failure,
    )
    adapter.consume(
        _step(1, 'prior'),
        _protocol_output({'systemMessage': 'prior message'}),
    )

    control = adapter.consume(
        _step(2, 'mutator'),
        _protocol_output(
            {
                'systemMessage': 'tentative message',
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow',
                    'updatedInput': {'command': 'replacement'},
                },
            }
        ),
    )

    assert (control == 'stop') is (not allow_on_failure)
    final = _response_bytes(adapter)
    response = load_object(final)
    assert 'prior message' in cast('str', response['systemMessage'])
    assert 'tentative message' not in final.decode()
    if allow_on_failure:
        assert 'hookSpecificOutput' not in response
    else:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert 'updatedInput' not in hook
    state = vars(adapter.state)
    assert state['payload'] == original_payload
    assert state['updated_input'] is None
    assert state['updated_input_present'] is False
    assert adapter.step_input() == raw_input
    diagnostic = stream.getvalue()
    assert diagnostic.count('malformed') == 1
    assert 'tentative message' not in diagnostic


@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_other_nonzero_exit_reports_and_continues(
    event_fixture: str,
) -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes(event_fixture))

    control = adapter.consume(_step(1, 'failed'), _output(returncode=7))

    assert control == 'continue'
    assert 'status 7' in _response_message(adapter)
    assert 'status 7' in stream.getvalue()


@pytest.mark.parametrize('failure_kind', list(OutputFailureKind))
@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_output_failure_uses_complete_common_matrix(
    event_fixture: str,
    allow_on_failure: bool,
    failure_kind: OutputFailureKind,
) -> None:
    raw_input = _fixture_bytes(event_fixture)
    payload = load_object(raw_input)
    stream = io.StringIO()
    adapter = _OutputFailureAdapter(
        raw_input,
        payload,
        Diagnostics(stream),
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume_output_failure(
        _step(1, 'failed'),
        failure_kind,
    )

    should_continue = {
        ('codex_pre.json', OutputFailureKind.MALFORMED, False): False,
        ('codex_pre.json', OutputFailureKind.MALFORMED, True): True,
        ('codex_pre.json', OutputFailureKind.UNSUPPORTED, False): False,
        ('codex_pre.json', OutputFailureKind.UNSUPPORTED, True): True,
        ('codex_pre.json', OutputFailureKind.OVERSIZED, False): False,
        ('codex_pre.json', OutputFailureKind.OVERSIZED, True): True,
        ('codex_pre.json', OutputFailureKind.UNDELIVERABLE_MUTATION, False): False,
        ('codex_pre.json', OutputFailureKind.UNDELIVERABLE_MUTATION, True): False,
        ('codex_post.json', OutputFailureKind.MALFORMED, False): True,
        ('codex_post.json', OutputFailureKind.MALFORMED, True): True,
        ('codex_post.json', OutputFailureKind.UNSUPPORTED, False): True,
        ('codex_post.json', OutputFailureKind.UNSUPPORTED, True): True,
        ('codex_post.json', OutputFailureKind.OVERSIZED, False): True,
        ('codex_post.json', OutputFailureKind.OVERSIZED, True): True,
        ('codex_post.json', OutputFailureKind.UNDELIVERABLE_MUTATION, False): True,
        ('codex_post.json', OutputFailureKind.UNDELIVERABLE_MUTATION, True): True,
    }[(event_fixture, failure_kind, allow_on_failure)]
    assert (control == 'continue') is should_continue
    response = load_object(_response_bytes(adapter))
    category = failure_kind.value.replace('_', ' ')
    assert category in cast('str', response['systemMessage'])
    if not should_continue:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert category in cast('str', hook['permissionDecisionReason'])
    assert category in stream.getvalue()


def test_codex_post_block_is_terminal_feedback() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_post.json'))

    control = adapter.consume(
        _step(1, 'reviewer'),
        _protocol_output({'decision': 'block', 'reason': 'bad result'}),
    )

    assert control == 'stop'
    assert load_object(adapter.finalize().stdout) == {
        'decision': 'block',
        'reason': '[step 1: reviewer]\nbad result',
    }


def test_codex_post_combines_native_feedback_context_and_message() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_post.json'))

    control = adapter.consume(
        _step(1, 'reviewer'),
        _protocol_output(
            {
                'decision': 'block',
                'reason': 'bad result',
                'continue': False,
                'stopReason': 'stop chain',
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
    assert response['decision'] == 'block'
    assert response['reason'] == '[step 1: reviewer]\nbad result'
    assert response['continue'] is False
    assert response['stopReason'] == '[step 1: reviewer]\nstop chain'
    assert response['systemMessage'] == '[step 1: reviewer]\nuser message'
    assert hook['additionalContext'] == '[step 1: reviewer]\nmodel context'


def test_codex_post_continue_false_is_terminal_with_owned_stop_reason() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_post.json'))

    control = adapter.consume(
        _step(1, 'stopper'),
        _protocol_output({'continue': False, 'stopReason': 'stop chain'}),
    )

    assert control == 'stop'
    assert load_object(adapter.finalize().stdout) == {
        'continue': False,
        'stopReason': '[step 1: stopper]\nstop chain',
    }


def test_codex_post_continue_true_has_no_control_effect() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_post.json'))

    control = adapter.consume(
        _step(1),
        _protocol_output({'continue': True, 'systemMessage': 'message'}),
    )

    assert control == 'continue'
    assert load_object(adapter.finalize().stdout) == {'systemMessage': '[step 1: step-1]\nmessage'}


def test_codex_post_failure_does_not_claim_to_undo_completed_action() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_post.json'))

    control = adapter.consume(_step(1), _output(b'{broken'))

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    assert set(response) == {'systemMessage'}
    assert 'malformed' in cast('str', response['systemMessage'])


def test_codex_pre_context_and_system_message_compose_in_step_order() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    adapter.consume(_step(1, 'context-one'), _pre_protocol(additional_context='one'))
    adapter.consume(
        _step(2, 'message'),
        _protocol_output({'systemMessage': 'message'}),
    )

    control = adapter.consume(
        _step(3, 'context-two'),
        _pre_protocol(additional_context='two'),
    )

    assert control == 'continue'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['additionalContext'] == (
        '[step 1: context-one]\none\n\n[step 3: context-two]\ntwo'
    )
    assert response['systemMessage'] == '[step 2: message]\nmessage'


def test_codex_pre_deny_is_terminal_and_prevents_later_step() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    executor = _QueueExecutor(
        [
            _pre_protocol('deny', reason='terminal'),
            _protocol_output({'systemMessage': 'must not run'}),
        ]
    )
    steps = (_step(1, 'blocker'), _step(2, 'later'))

    response = run_pipeline(
        adapter,
        steps,
        executor,
        None,
        allow_on_failure=False,
    )

    assert executor.calls == [steps[0]]
    assert b'terminal' in response.stdout
    assert b'must not run' not in response.stdout


def test_codex_pre_deny_reasons_include_only_winning_rank_in_step_order() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    adapter.consume(
        _step(1, 'mutator'),
        _pre_protocol(
            'allow',
            reason='allow reason',
            updated_input={'command': 'safe'},
        ),
    )
    adapter.consume(
        _step(2, 'context'),
        _pre_protocol(additional_context='context only'),
    )

    control = adapter.consume(
        _step(3, 'blocker'),
        _pre_protocol('deny', reason='deny reason'),
    )

    assert control == 'stop'
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecision'] == 'deny'
    assert hook['permissionDecisionReason'] == labeled(
        _step(3, 'blocker'),
        'deny reason',
    )
    assert 'updatedInput' not in hook
    assert 'additionalContext' not in hook


def test_codex_pre_empty_stdout_is_neutral() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))

    control = adapter.consume(_step(1, 'neutral'), _output())

    assert control == 'continue'
    assert adapter.finalize().stdout == b''


@pytest.mark.parametrize(
    ('tool_name', 'replacement'),
    [
        ('Bash', {'command': 'printf safe'}),
        ('apply_patch', {'command': '*** Begin Patch\n*** End Patch'}),
        ('mcp__server__tool', {'value': 1}),
        ('local_function', {'value': 1}),
    ],
)
def test_codex_pre_replacement_accepts_each_tool_kind(
    tool_name: str,
    replacement: JsonObject,
) -> None:
    adapter = _make_adapter(_input_for_tool(tool_name))

    control = adapter.consume(
        _step(1),
        _pre_protocol('allow', updated_input=replacement),
    )

    assert control == 'continue'
    assert load_object(adapter.step_input())['tool_input'] == replacement
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['permissionDecision'] == 'allow'
    assert hook['updatedInput'] == replacement


def test_codex_pre_replacement_is_whole_sequential_and_preserves_unknown_input() -> None:
    raw_input = _fixture_bytes('codex_pre.json')
    adapter = _make_adapter(raw_input)
    first = {'command': 'printf first', 'first_only': True}
    second = {'command': 'printf second', 'second_only': True}

    adapter.consume(_step(1), _pre_protocol('allow', updated_input=first))
    first_input = load_object(adapter.step_input())
    adapter.consume(_step(2), _pre_protocol('allow', updated_input=second))
    second_input = load_object(adapter.step_input())

    assert first_input['tool_input'] == first
    assert second_input['tool_input'] == second
    assert 'first_only' not in cast('dict[str, object]', second_input['tool_input'])
    assert second_input['unknown_top_level'] == {'preserve': True}
    hook = cast(
        'dict[str, object]',
        load_object(adapter.finalize().stdout)['hookSpecificOutput'],
    )
    assert hook['updatedInput'] == second


@pytest.mark.parametrize(
    ('tool_name', 'replacement'),
    [
        ('Bash', {}),
        ('Bash', {'command': 1}),
        ('apply_patch', {'patch': 'missing command'}),
        ('mcp__server__tool', []),
        ('local_function', 'not arguments'),
    ],
)
def test_codex_pre_replacement_validates_tool_kind(
    tool_name: str,
    replacement: JsonValue,
) -> None:
    raw_input = _input_for_tool(tool_name)
    adapter = _make_adapter(raw_input)

    control = adapter.consume(
        _step(1, 'mutator'),
        _pre_protocol('allow', updated_input=replacement),
    )

    assert control == 'stop'
    assert 'unsupported' in _response_message(adapter)
    assert adapter.step_input() == raw_input


def test_codex_reachable_mutation_exact_final_boundary_is_accepted() -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))
    mutation = _accept_exact_boundary_mutation(adapter)

    assert load_object(adapter.step_input())['tool_input'] == mutation


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_codex_reachable_mutation_one_byte_over_rolls_back_after_apply(
    allow_on_failure: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_pre.json'),
        allow_on_failure=allow_on_failure,
    )
    prior_mutation: JsonObject = {}
    for index in range(1, _BOUNDARY_PRIOR_COUNT + 1):
        prior_mutation = {'command': f'prior-{index}'}
        adapter.consume(
            _step(index, f'prior-{index}'),
            _pre_protocol(
                'allow',
                reason=_BOUNDARY_PRIOR_REASON,
                updated_input=prior_mutation,
            ),
        )
    prior_input = adapter.step_input()
    mutation, _expected = _boundary_mutation(1)
    candidate = _pre_protocol(
        'allow',
        additional_context='tentative context',
        reason=_BOUNDARY_CANDIDATE_REASON,
        updated_input=mutation,
    )
    assert len(candidate.stdout) < _STDOUT_LIMIT_BYTES

    control = adapter.consume(
        _step(_BOUNDARY_PRIOR_COUNT + 1, 'candidate'),
        candidate,
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    hook = cast('dict[str, object]', response['hookSpecificOutput'])
    assert hook['permissionDecision'] == 'deny'
    assert 'undeliverable mutation' in cast(
        'str',
        hook['permissionDecisionReason'],
    )
    assert 'updatedInput' not in hook
    assert _BOUNDARY_CANDIDATE_REASON not in adapter.finalize().stdout.decode()
    state = vars(adapter.state)
    assert state['payload'] == load_object(prior_input)
    assert state['updated_input'] == prior_mutation
    assert state['updated_input_present'] is True
    assert adapter.step_input() == prior_input
    diagnostic = stream.getvalue()
    assert diagnostic.count('undeliverable mutation') == 1
    assert 'tentative context' not in diagnostic


@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_repeated_status_and_failure_category_keep_each_attribution(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=True,
    )
    repeated_results = (
        _output(returncode=43),
        _output(returncode=43),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'FIRST_TIMEOUT_DETAIL_61'),
            ContainmentState.CONTAINED,
        ),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'SECOND_TIMEOUT_DETAIL_67'),
            ContainmentState.CONTAINED,
        ),
    )

    for index, result in enumerate(repeated_results, 1):
        assert adapter.consume(_step(index, f'REPEATED_SENTINEL_{index}'), result) == 'continue'

    assert load_object(adapter.finalize().stdout)['systemMessage'] == (
        '[step 1: REPEATED_SENTINEL_1]\n'
        'exited with status 43; see developer diagnostics\n\n'
        '[step 2: REPEATED_SENTINEL_2]\n'
        'exited with status 43; see developer diagnostics\n\n'
        '[step 3: REPEATED_SENTINEL_3]\n'
        'timeout: child result rejected; see developer diagnostics\n\n'
        '[step 4: REPEATED_SENTINEL_4]\n'
        'timeout: child result rejected; see developer diagnostics'
    )


@pytest.mark.parametrize(
    ('event_fixture', 'allow_on_failure'),
    [
        ('codex_pre.json', True),
        ('codex_post.json', False),
        ('codex_post.json', True),
    ],
)
def test_codex_repeated_uncontained_terminations_warn_once(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    for index, failure_kind in enumerate(
        (FailureKind.TIMEOUT, FailureKind.OUTPUT_LIMIT),
        1,
    ):
        control = adapter.consume(
            _step(index, f'uncontained-{index}'),
            StepResult(
                None,
                b'',
                '',
                StepFailure(failure_kind, f'{failure_kind.value} detail'),
                ContainmentState.UNCONTAINED,
            ),
        )
        assert control == 'continue'

    message = cast(
        'str',
        load_object(adapter.finalize().stdout)['systemMessage'],
    )
    assert message.count('without containment') == 1
    assert 'timeout' in message
    assert 'output limit' in message
    diagnostic = stream.getvalue()
    assert diagnostic.count('without containment') == 1
    assert 'timeout' in diagnostic
    assert 'output limit' in diagnostic


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
def test_codex_required_payload_fields_accept_any_strict_json_value(
    raw_input: bytes,
) -> None:
    adapter = _make_adapter(raw_input)

    assert adapter.step_input() == raw_input


@pytest.mark.parametrize('prefix', [b'', b' ', b'\t\r\n'])
@pytest.mark.parametrize(
    ('stdout', 'classification'),
    [
        (b'', 'neutral'),
        (b'{"systemMessage":"native"}', 'structured'),
        (b'{broken', 'malformed'),
        (b'[broken', 'malformed'),
        (b'plain developer text', 'developer'),
        (b'"scalar"', 'developer'),
        (b'\xff', 'malformed'),
    ],
)
def test_codex_success_output_classification(
    prefix: bytes,
    stdout: bytes,
    classification: str,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes('codex_post.json'),
    )

    control = adapter.consume(_step(1, 'classifier'), _output(prefix + stdout))

    assert control == 'continue'
    final = adapter.finalize().stdout
    diagnostic = stream.getvalue()
    if classification == 'neutral':
        assert final == b''
        assert diagnostic == ''
    elif classification == 'structured':
        assert b'native' in final
        assert diagnostic == ''
    elif classification == 'developer':
        assert final == b''
        assert stdout not in final
        assert 'developer stdout omitted' in diagnostic
    else:
        assert b'malformed' in final
        assert 'malformed' in diagnostic


@pytest.mark.parametrize(
    'failure_kind',
    [FailureKind.TIMEOUT, FailureKind.OUTPUT_LIMIT],
)
@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_uncontained_termination_reports_reduced_guarantee(
    event_fixture: str,
    allow_on_failure: bool,
    failure_kind: FailureKind,
) -> None:
    adapter, stream = _make_adapter_with_stream(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    result = StepResult(
        None,
        b'',
        '',
        StepFailure(failure_kind, f'{failure_kind.value} detail'),
        ContainmentState.UNCONTAINED,
    )

    control = adapter.consume(_step(1, 'uncontained'), result)

    should_stop = event_fixture == 'codex_pre.json' and not allow_on_failure
    assert (control == 'stop') is should_stop
    response = load_object(_response_bytes(adapter))
    message = cast('str', response['systemMessage'])
    category = failure_kind.value.replace('_', ' ')
    assert 'without containment' in message
    assert category in message
    if should_stop:
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert category in cast('str', hook['permissionDecisionReason'])
    diagnostic = stream.getvalue()
    assert diagnostic.count('without containment') == 1
    assert category in diagnostic


@pytest.mark.parametrize(
    'value',
    [
        {'decision': 'allow'},
        {'reason': 'orphan'},
        {'stopReason': 'orphan'},
        {'continue': 'false'},
        {'suppressOutput': True},
        {'terminalSequence': 'sequence'},
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'additionalContext': 'wrong event',
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'updatedMCPToolOutput': {},
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'updatedInput': {},
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'unknownMutation': {},
            }
        },
        {'unknown': True},
    ],
)
def test_codex_unsupported_post_output_reports_and_continues(
    value: JsonObject,
) -> None:
    adapter, stream = _make_adapter_with_stream(_fixture_bytes('codex_post.json'))

    control = adapter.consume(_step(1, 'unsupported'), _protocol_output(value))

    assert control == 'continue'
    assert 'unsupported' in _response_message(adapter)
    assert stream.getvalue().count('sequential-hooks: step 1 (unsupported):') == 1


@pytest.mark.parametrize(
    'value',
    [
        {'hookSpecificOutput': None},
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': None,
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'ask',
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'updatedInput': {'command': 'orphan'},
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'updatedInput': {'command': 'forbidden'},
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'permissionDecision': 'deny',
            }
        },
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'updatedMCPToolOutput': {},
            }
        },
        {'continue': False},
        {'suppressOutput': True},
        {'terminalSequence': 'sequence'},
        {'unknown': True},
    ],
)
def test_codex_unsupported_pre_output_follows_failure_policy(
    value: JsonObject,
) -> None:
    adapter = _make_adapter(_fixture_bytes('codex_pre.json'))

    control = adapter.consume(_step(1, 'unsupported'), _protocol_output(value))

    assert control == 'stop'
    assert 'unsupported' in _response_message(adapter)


@pytest.mark.parametrize('event_fixture', ['codex_pre.json', 'codex_post.json'])
def test_codex_user_contributions_interleave_native_and_mandatory_messages(
    event_fixture: str,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=True,
    )

    assert (adapter.consume(_step(1, 'FIRST_EXIT_41'), _output(returncode=41))) == 'continue'
    assert (
        adapter.consume(
            _step(2, 'MIDDLE_NATIVE_59'),
            _protocol_output({'systemMessage': 'NATIVE_SENTINEL_59'}),
        )
    ) == 'continue'
    assert (
        adapter.consume(
            _step(3, 'LAST_TIMEOUT_83'),
            StepResult(
                None,
                b'',
                '',
                StepFailure(FailureKind.TIMEOUT, 'TIMEOUT_DETAIL_83'),
                ContainmentState.CONTAINED,
            ),
        )
    ) == 'continue'

    assert load_object(adapter.finalize().stdout)['systemMessage'] == (
        '[step 1: FIRST_EXIT_41]\n'
        'exited with status 41; see developer diagnostics\n\n'
        '[step 2: MIDDLE_NATIVE_59]\n'
        'NATIVE_SENTINEL_59\n\n'
        '[step 3: LAST_TIMEOUT_83]\n'
        'timeout: child result rejected; see developer diagnostics'
    )


@pytest.mark.parametrize(
    ('event_fixture', 'allow_on_failure'),
    [
        ('codex_pre.json', True),
        ('codex_post.json', False),
        ('codex_post.json', True),
    ],
)
def test_codex_user_contributions_retain_repeated_reports_in_global_order(
    event_fixture: str,
    allow_on_failure: bool,
) -> None:
    adapter = _make_adapter(
        _fixture_bytes(event_fixture),
        allow_on_failure=allow_on_failure,
    )
    results = (
        _output(returncode=7),
        _protocol_output({'systemMessage': 'OPTIONAL_NATIVE_MUST_SHED_31'}),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'first timeout'),
            ContainmentState.CONTAINED,
        ),
        _output(returncode=7),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'second timeout'),
            ContainmentState.UNCONTAINED,
        ),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.OUTPUT_LIMIT, 'output limit detail'),
            ContainmentState.UNCONTAINED,
        ),
    )
    for index, result in enumerate(results, 1):
        assert adapter.consume(_step(index), result) == 'continue'

    control = adapter.consume(
        _step(len(results) + 1, _IMPOSSIBLE_STEP_NAME),
        _output(returncode=2, blocking_stderr=_EXIT_TWO_SENTINEL),
    )

    assert control == 'stop'
    response = load_object(adapter.finalize().stdout)
    message = cast('str', response['systemMessage'])
    expected = (
        'exited with status 7; see developer diagnostics\n\n'
        'timeout: see developer diagnostics\n\n'
        'exited with status 7; see developer diagnostics\n\n'
        'step ran without containment; descendant cleanup is not guaranteed\n\n'
        'timeout: see developer diagnostics\n\n'
        'output limit: see developer diagnostics'
    )
    assert message == expected
    if event_fixture == 'codex_pre.json':
        hook = cast('dict[str, object]', response['hookSpecificOutput'])
        assert hook['permissionDecision'] == 'deny'
        assert hook['permissionDecisionReason'] == _EXIT_TWO_SENTINEL
    else:
        assert response['decision'] == 'block'
        assert response['reason'] == _EXIT_TWO_SENTINEL
    assert _IMPOSSIBLE_STEP_NAME.encode() not in adapter.finalize().stdout
    assert b'OPTIONAL_NATIVE_MUST_SHED_31' not in adapter.finalize().stdout
    assert len(adapter.finalize().stdout) <= FINAL_RESPONSE_LIMIT_BYTES


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_configuration_response_is_independent_of_adapter_construction(
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('configuration response must not construct an adapter')

    monkeypatch.setattr('plugins.codex._sequential_hooks._adapter._CodexAdapter', unavailable)
    monkeypatch.setattr(CodexHost, 'adapter', unavailable)
    stream = io.StringIO()
    response = CodexHost().configuration_response(
        event, 'invalid configuration', Diagnostics(stream)
    )
    message = '[step 1: configuration]\nspawn: child result rejected; see developer diagnostics'
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
    host = CodexHost()

    assert type(host) is CodexHost
    assert (host.api_version, host.name, host.events, host.requires_event) == (
        API_VERSION,
        'codex',
        ('PreToolUse', 'PostToolUse'),
        False,
    )
