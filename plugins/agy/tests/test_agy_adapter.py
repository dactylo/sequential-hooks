# pyright: reportPrivateUsage=false

import io
import json
from itertools import product
from pathlib import Path
from typing import cast

import pytest

from plugins.agy._sequential_hooks import _adapter as agy_module
from plugins.agy._sequential_hooks._adapter import AgyAdapter, AgyHost
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._executor import _STDOUT_LIMIT_BYTES
from sequential_hooks._types import (
    ContainmentState,
    FailureKind,
    Step,
    StepFailure,
    StepResult,
)
from sequential_hooks.hosts import (
    API_VERSION,
    FINAL_RESPONSE_LIMIT_BYTES,
    AdapterOutputError,
    ChainControl,
    HostInputError,
    JsonObject,
    JsonValue,
    OutputFailureKind,
)
from sequential_hooks.hosts._json import load_object

_FIXTURE_ROOT = Path(__file__).parent / 'fixtures'
_PRE_RANK = {
    'neutral': 0,
    'allow': 1,
    'ask': 2,
    'force_ask': 3,
    'deny': 4,
}
_UNCONTAINED_WARNING = 'step ran without containment; descendant cleanup is not guaranteed'


class _OutputFailureAdapter(AgyAdapter):
    def consume_output_failure(
        self,
        step: Step,
        failure: OutputFailureKind,
    ) -> ChainControl:
        return self._apply_failure(step, failure.value, f'{failure.value} detail')


def _encoded(value: JsonObject) -> bytes:
    return f'{json.dumps(value, separators=(",", ":"))}\n'.encode()


def _fixture(event: str) -> bytes:
    name = 'agy_pre.json' if event == 'PreToolUse' else 'agy_post.json'
    return (_FIXTURE_ROOT / name).read_bytes()


def _adapter(
    event: str,
    raw_input: bytes | None = None,
    *,
    allow_on_failure: bool = False,
) -> tuple[AgyAdapter, io.StringIO]:
    data = _fixture(event) if raw_input is None else raw_input
    stream = io.StringIO()
    return (
        AgyAdapter(
            event,
            data,
            load_object(data),
            Diagnostics(stream),
            allow_on_failure=allow_on_failure,
        ),
        stream,
    )


def _output(value: JsonObject) -> StepResult:
    stdout = f'{json.dumps(value, separators=(",", ":"))}\n'.encode()
    assert len(stdout) <= _STDOUT_LIMIT_BYTES
    return StepResult(0, stdout, '')


def _output_failure_adapter(
    event: str,
    *,
    allow_on_failure: bool,
) -> tuple[_OutputFailureAdapter, io.StringIO]:
    data = _fixture(event)
    stream = io.StringIO()
    return (
        _OutputFailureAdapter(
            event,
            data,
            load_object(data),
            Diagnostics(stream),
            allow_on_failure=allow_on_failure,
        ),
        stream,
    )


def _response(adapter: AgyAdapter) -> JsonObject:
    return load_object(adapter.finalize().stdout)


def _step(index: int) -> Step:
    return Step(index, (f'step-{index}',))


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_accepts_realistic_input_and_preserves_exact_step_input(event: str) -> None:
    raw_input = _fixture(event)
    adapter, _stream = _adapter(event)

    assert adapter.event == event
    assert adapter.should_run_steps is True
    assert adapter.step_input() == raw_input


def test_all_neutral_chain_emits_canonical_allow() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(_step(1), StepResult(0, b'', ''))
    adapter.consume(_step(2), _output({'decision': 'neutral'}))

    assert adapter.finalize().stdout == b'{"decision":"allow"}\n'


def test_conflicting_winning_overrides_become_terminal_deny() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'permissionOverrides': ['one']}),
    )

    control = adapter.consume(
        _step(2),
        _output({'decision': 'ask', 'permissionOverrides': ['two']}),
    )

    assert control == 'stop'
    response = _response(adapter)
    assert response['decision'] == 'deny'
    assert 'step 1' in cast('str', response['reason'])
    assert 'step 2' in cast('str', response['reason'])
    assert 'one' not in cast('str', response['reason'])
    assert 'two' not in cast('str', response['reason'])
    assert 'permissionOverrides' not in response


def test_deny_is_terminal() -> None:
    adapter, _stream = _adapter('PreToolUse')

    control = adapter.consume(_step(1), _output({'decision': 'deny'}))

    assert control == 'stop'
    assert adapter.finalize().stdout == b'{"decision":"deny"}\n'


def test_deny_omits_overrides() -> None:
    adapter, _stream = _adapter('PreToolUse')

    adapter.consume(
        _step(1),
        _output({'decision': 'deny', 'permissionOverrides': ['ignored']}),
    )

    assert 'permissionOverrides' not in _response(adapter)


def test_differently_ordered_equal_members_conflict_without_payload_leakage() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(8),
        _output({'decision': 'ask', 'permissionOverrides': ['ALPHA', 'BETA']}),
    )

    control = adapter.consume(
        _step(9),
        _output({'decision': 'ask', 'permissionOverrides': ['BETA', 'ALPHA']}),
    )

    assert control == 'stop'
    response_bytes = adapter.finalize().stdout
    response_text = response_bytes.decode('utf-8')
    _response_object, response_end = json.JSONDecoder().raw_decode(response_text)
    assert response_text[response_end:] == '\n'
    assert load_object(response_bytes) == {
        'decision': 'deny',
        'reason': (
            '[step 9: step-9]\n'
            'conflicting permissionOverrides from contributing steps: step 8, step 9'
        ),
    }
    assert b'ALPHA' not in response_bytes
    assert b'BETA' not in response_bytes


def test_empty_override_before_conflicting_arrays_does_not_hide_conflict() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'permissionOverrides': []}),
    )
    adapter.consume(
        _step(2),
        _output({'decision': 'ask', 'permissionOverrides': ['ONE']}),
    )

    control = adapter.consume(
        _step(3),
        _output({'decision': 'ask', 'permissionOverrides': ['TWO']}),
    )

    assert control == 'stop'
    assert adapter.finalize().stdout == (
        b'{"decision":"deny","reason":"[step 3: step-3]\\n'
        b'conflicting permissionOverrides from contributing steps: step 2, step 3"}\n'
    )


def test_empty_permission_overrides_are_omitted() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'permissionOverrides': []}),
    )

    assert adapter.finalize().stdout == b'{"decision":"ask"}\n'


@pytest.mark.parametrize(
    ('first', 'second'),
    list(product(_PRE_RANK, repeat=2)),
)
def test_every_ordered_decision_pair_has_exact_rank_and_reason_ownership(
    first: str,
    second: str,
) -> None:
    adapter, _stream = _adapter('PreToolUse')
    first_reason = f'FIRST_{first}'
    second_reason = f'SECOND_{second}'

    control = adapter.consume(
        _step(1),
        _output({'decision': first, 'reason': first_reason}),
    )
    if control != 'stop':
        control = adapter.consume(
            _step(2),
            _output({'decision': second, 'reason': second_reason}),
        )

    if first == 'deny':
        winner = 'deny'
        reasons = [f'[step 1: step-1]\n{first_reason}']
    elif _PRE_RANK[second] > _PRE_RANK[first]:
        winner = second
        reasons = [f'[step 2: step-2]\n{second_reason}']
    elif _PRE_RANK[second] == _PRE_RANK[first]:
        winner = first
        reasons = [
            f'[step 1: step-1]\n{first_reason}',
            f'[step 2: step-2]\n{second_reason}',
        ]
    else:
        winner = first
        reasons = [f'[step 1: step-1]\n{first_reason}']
    native_winner = 'allow' if winner == 'neutral' else winner
    expected = {
        'decision': native_winner,
        'reason': '\n\n'.join(reasons),
    }
    assert _response(adapter) == expected


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_exit_zero_silence_is_neutral(event: str) -> None:
    adapter, stream = _adapter(event)

    assert adapter.consume(_step(1), StepResult(0, b'', '')) == 'continue'
    assert stream.getvalue() == ''


def test_explicit_adapter_event_ignores_payload_event_clues() -> None:
    payload = load_object(_fixture('PreToolUse'))
    payload['hook_event_name'] = 'PostToolUse'
    raw_input = f'{json.dumps(payload)}\n'.encode()

    adapter, _stream = _adapter('PreToolUse', raw_input)

    assert adapter.event == 'PreToolUse'


def test_host_contract_metadata() -> None:
    host = AgyHost()

    assert type(host) is AgyHost
    assert (host.api_version, host.name, host.events, host.requires_event) == (
        API_VERSION,
        'agy',
        ('PreToolUse', 'PostToolUse'),
        True,
    )


def test_immediate_stop_retains_its_terminal_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _stream = _adapter('PreToolUse')
    real_encode_response = agy_module.encode_response
    encodings: list[bytes] = []

    def counted_encode_response(response: JsonObject) -> bytes:
        encoded = real_encode_response(response)
        encodings.append(encoded)
        return encoded

    monkeypatch.setattr(agy_module, 'encode_response', counted_encode_response)

    control = adapter.consume(
        _step(1),
        _output({'decision': 'deny', 'reason': 'terminal'}),
    )

    assert control == 'stop'
    assert adapter.finalize().stdout is encodings[0]
    assert len(encodings) == 1

    assert adapter.finalize().stdout is encodings[0]
    assert len(encodings) == 1


@pytest.mark.parametrize(
    ('event', 'allow_on_failure', 'failure'),
    [
        (event, allow_on_failure, failure)
        for event, allow_on_failure, failure in product(
            ('PreToolUse', 'PostToolUse'),
            (False, True),
            tuple(FailureKind),
        )
    ],
)
def test_infrastructure_failure_matrix(
    event: str,
    allow_on_failure: bool,
    failure: FailureKind,
) -> None:
    adapter, stream = _adapter(event, allow_on_failure=allow_on_failure)
    category = failure.value.replace('_', ' ')
    if event == 'PreToolUse' and allow_on_failure:
        adapter.consume(
            _step(3),
            _output({'decision': 'ask', 'reason': 'NATIVE_MATRIX_REASON'}),
        )

    control = adapter.consume(
        _step(4),
        StepResult(None, b'', '', StepFailure(failure, 'matrix detail')),
    )

    expected_continue = event == 'PostToolUse' or allow_on_failure
    assert control == ('continue' if expected_continue else 'stop')
    expected_stdout = (
        b'{}\n'
        if event == 'PostToolUse'
        else _encoded(
            {
                'decision': 'ask',
                'reason': (
                    '[step 3: step-3]\nNATIVE_MATRIX_REASON\n\n'
                    f'[step 4: step-4]\n{category}: matrix detail'
                ),
            }
        )
        if allow_on_failure
        else _encoded(
            {
                'decision': 'deny',
                'reason': f'[step 4: step-4]\n{category}: matrix detail',
            }
        )
    )
    actual = adapter.finalize().stdout
    assert actual == expected_stdout
    assert stream.getvalue() == (f'sequential-hooks: step 4 (step-4): {category}: matrix detail\n')


@pytest.mark.parametrize(
    'value',
    [
        {},
        {'reason': 'missing decision'},
        {'decision': 'allow', 'unknown': True},
        {'decision': 'allow', 'updatedInput': {}},
        {'decision': 'bogus'},
        {'decision': 1},
        {'decision': 'allow', 'reason': None},
        {'decision': 'allow', 'reason': 1},
        {'decision': 'allow', 'permissionOverrides': None},
    ],
)
def test_invalid_pre_tool_objects_are_rejected(value: JsonObject) -> None:
    adapter, _stream = _adapter('PreToolUse')

    control = adapter.consume(_step(1), _output(value))

    assert control == 'stop'
    assert _response(adapter)['decision'] == 'deny'


def test_lone_surrogate_uncontained_failure_restores_exact_prior_state() -> None:
    raw_input = _fixture('PreToolUse')
    payload = load_object(raw_input)
    stream = io.StringIO()
    adapter = AgyAdapter(
        'PreToolUse',
        raw_input,
        payload,
        Diagnostics(stream),
        allow_on_failure=True,
    )
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'reason': 'PRIOR_REASON'}),
    )
    adapter.consume(
        _step(2),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'first timeout'),
            ContainmentState.UNCONTAINED,
        ),
    )
    prior_state = vars(adapter)
    prior_raw_input = adapter.step_input()
    prior_warnings = prior_state['_warnings']
    prior_warning_values = list(cast('list[str]', prior_warnings))
    prior_winning = prior_state['_winning']
    prior_rank = prior_state['_winning_rank']
    prior_uncontained_state = prior_state['_uncontained_warning_stored']
    prior_response = adapter.finalize().stdout
    stream.seek(0)
    stream.truncate()

    with pytest.raises(AdapterOutputError) as raised:
        adapter.consume(
            Step(3, ('surrogate-\ud800',)),
            StepResult(
                None,
                b'',
                '',
                StepFailure(FailureKind.OUTPUT_LIMIT, 'output limit'),
                ContainmentState.UNCONTAINED,
            ),
        )

    assert raised.value.kind is OutputFailureKind.MALFORMED
    restored = vars(adapter)
    assert adapter.step_input() is prior_raw_input
    assert load_object(adapter.step_input()) == payload
    assert restored['_warnings'] is prior_warnings
    assert restored['_warnings'] == prior_warning_values
    assert restored['_winning'] is prior_winning
    assert restored['_winning_rank'] == prior_rank
    assert restored['_uncontained_warning_stored'] is prior_uncontained_state
    assert adapter.finalize().stdout == prior_response
    assert stream.getvalue().count(_UNCONTAINED_WARNING) == 1


def test_multi_step_rank_transitions_drop_weaker_reasons_in_step_order() -> None:
    adapter, _stream = _adapter('PreToolUse')
    for index, decision in enumerate(
        ('neutral', 'ask', 'allow', 'ask', 'force_ask', 'ask', 'force_ask'),
        1,
    ):
        adapter.consume(
            _step(index),
            _output({'decision': decision, 'reason': f'REASON_{index}'}),
        )

    assert adapter.finalize().stdout == (
        b'{"decision":"force_ask","reason":"[step 5: step-5]\\nREASON_5'
        b'\\n\\n[step 7: step-7]\\nREASON_7"}\n'
    )


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
@pytest.mark.parametrize(
    'stdout',
    [
        b'text',
        b'{',
        b'[',
        b'1',
        b'{}{}',
        b'{} trailing',
        b'\xff',
    ],
)
def test_nonempty_unparsable_output_is_malformed(
    event: str,
    stdout: bytes,
) -> None:
    adapter, stream = _adapter(event, allow_on_failure=True)

    control = adapter.consume(_step(1), StepResult(0, stdout, ''))

    assert 'malformed' in stream.getvalue()
    if event == 'PreToolUse':
        assert control == 'continue'
    else:
        assert control == 'continue'
        assert adapter.finalize().stdout == b'{}\n'


def test_optional_reason_sheds_while_mandatory_overrides_survive_exactly() -> None:
    adapter, _stream = _adapter('PreToolUse')
    overrides: list[JsonValue] = ['SECOND', 'FIRST']
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'reason': 'R' * (FINAL_RESPONSE_LIMIT_BYTES // 2)}),
    )

    control = adapter.consume(
        _step(2),
        _output(
            {
                'decision': 'ask',
                'reason': 'R' * (FINAL_RESPONSE_LIMIT_BYTES // 2),
                'permissionOverrides': overrides,
            }
        ),
    )

    assert control == 'continue'
    assert adapter.finalize().stdout == (
        b'{"decision":"ask","permissionOverrides":["SECOND","FIRST"]}\n'
    )


@pytest.mark.parametrize(
    ('event', 'allow_on_failure', 'failure'),
    [
        (event, allow_on_failure, failure)
        for event, allow_on_failure, failure in product(
            ('PreToolUse', 'PostToolUse'),
            (False, True),
            (
                OutputFailureKind.MALFORMED,
                OutputFailureKind.OVERSIZED,
                OutputFailureKind.UNSUPPORTED,
            ),
        )
    ],
)
def test_output_failure_matrix(
    event: str,
    allow_on_failure: bool,
    failure: OutputFailureKind,
) -> None:
    adapter, stream = _output_failure_adapter(
        event,
        allow_on_failure=allow_on_failure,
    )
    category = failure.value.replace('_', ' ')
    if event == 'PreToolUse' and allow_on_failure:
        adapter.consume(
            _step(3),
            _output({'decision': 'ask', 'reason': 'NATIVE_MATRIX_REASON'}),
        )

    control = adapter.consume_output_failure(_step(5), failure)

    expected_continue = event == 'PostToolUse' or allow_on_failure
    assert control == ('continue' if expected_continue else 'stop')
    expected_stdout = (
        b'{}\n'
        if event == 'PostToolUse'
        else _encoded(
            {
                'decision': 'ask',
                'reason': (
                    '[step 3: step-3]\nNATIVE_MATRIX_REASON\n\n'
                    f'[step 5: step-5]\n{category}: {failure.value} detail'
                ),
            }
        )
        if allow_on_failure
        else _encoded(
            {
                'decision': 'deny',
                'reason': (f'[step 5: step-5]\n{category}: {failure.value} detail'),
            }
        )
    )
    actual = adapter.finalize().stdout
    assert actual == expected_stdout
    assert stream.getvalue() == (
        f'sequential-hooks: step 5 (step-5): {category}: {failure.value} detail\n'
    )


@pytest.mark.parametrize(
    'overrides',
    [True, 1, 1.5, 'value', {}, [None], [1], [True], [{}], [['nested']]],
)
@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_override_wrong_types_follow_exact_malformed_policy(
    overrides: JsonValue,
    allow_on_failure: bool,
) -> None:
    adapter, stream = _adapter(
        'PreToolUse',
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'permissionOverrides': overrides}),
    )

    assert control == ('continue' if allow_on_failure else 'stop')
    assert (adapter.finalize().stdout) == (
        b'{"decision":"allow"}\n'
        if allow_on_failure
        else (
            b'{"decision":"deny","reason":"[step 1: step-1]\\n'
            b'malformed: Antigravity permissionOverrides must be an array of strings"}\n'
        )
    )
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (step-1): malformed: '
        'Antigravity permissionOverrides must be an array of strings\n'
    )


def test_post_parse_unencodable_reason_enters_failure_policy_atomically() -> None:
    adapter, stream = _adapter('PreToolUse')

    control = adapter.consume(
        _step(1),
        StepResult(0, b'{"decision":"ask","reason":"\\ud800"}\n', ''),
    )

    assert control == 'stop'
    assert adapter.finalize().stdout == (
        b'{"decision":"deny","reason":"[step 1: step-1]\\n'
        b'malformed: structured response is not valid UTF-8 text"}\n'
    )
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (step-1): malformed: '
        'structured response is not valid UTF-8 text\n'
    )


@pytest.mark.parametrize('value', [{}, {'decision': 'allow'}, {'message': 'no'}])
def test_post_tool_accepts_only_empty_object(value: JsonObject) -> None:
    adapter, stream = _adapter('PostToolUse')

    control = adapter.consume(_step(1), _output(value))

    if value:
        assert 'unsupported' in stream.getvalue()
    assert control == 'continue'
    assert adapter.finalize().stdout == b'{}\n'


@pytest.mark.parametrize(
    ('tool_call_present', 'tool_call', 'expected'),
    [
        (False, None, True),
        (True, None, False),
        (True, {}, True),
        (True, False, True),
    ],
)
def test_post_tool_applicability_uses_only_explicit_null(
    tool_call_present: bool,
    tool_call: JsonValue,
    expected: bool,
) -> None:
    payload: JsonObject = {'stepIdx': 1}
    if tool_call_present:
        payload['toolCall'] = tool_call
    raw_input = _encoded(payload)
    adapter, _stream = _adapter('PostToolUse', raw_input)

    assert adapter.should_run_steps is expected
    assert adapter.step_input() == raw_input


@pytest.mark.parametrize(
    ('step_index_present', 'step_index'),
    [
        (False, None),
        (True, 'wrong'),
    ],
)
def test_post_tool_explicit_null_still_requires_integer_step_idx(
    step_index_present: bool,
    step_index: JsonValue,
) -> None:
    payload: JsonObject = {'toolCall': None}
    if step_index_present:
        payload['stepIdx'] = step_index

    with pytest.raises(HostInputError, match='integer stepIdx'):
        _adapter('PostToolUse', _encoded(payload))


def test_post_tool_nonzero_exit_is_reported_discarded_and_chain_continues() -> None:
    adapter, stream = _adapter('PostToolUse')

    first = adapter.consume(
        _step(1),
        StepResult(7, b'{"decision":"deny"}\n', 'child failed'),
    )
    second = adapter.consume(_step(2), _output({}))

    assert first == 'continue'
    assert second == 'continue'
    assert 'status 7' in stream.getvalue()
    assert adapter.finalize().stdout == b'{}\n'


def test_post_tool_validates_only_step_idx() -> None:
    payload: JsonObject = {'stepIdx': 1}
    raw_input = f'{json.dumps(payload)}\n'.encode()

    adapter, _stream = _adapter('PostToolUse', raw_input)

    assert adapter.event == 'PostToolUse'


def test_pre_tool_failure_does_not_manufacture_warning_decision() -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)

    adapter.consume(
        _step(1),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timed out'),
        ),
    )

    assert adapter.finalize().stdout == b'{"decision":"allow"}\n'
    assert 'timed out' in stream.getvalue()


def test_pre_tool_failure_warning_rides_existing_reason_only() -> None:
    adapter, _stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(_step(1), _output({'decision': 'ask', 'reason': 'review'}))
    adapter.consume(
        _step(2),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timed out'),
        ),
    )

    response = load_object(adapter.finalize().stdout)
    assert response['decision'] == 'ask'
    assert 'timed out' in cast('str', response['reason'])


def test_pre_tool_three_step_rank_change_drops_weaker_reasons() -> None:
    adapter, _stream = _adapter('PreToolUse')

    for index, decision in enumerate(('ask', 'allow', 'force_ask'), 1):
        control = adapter.consume(
            _step(index),
            _output({'decision': decision, 'reason': decision}),
        )
        assert control == 'continue'

    assert _response(adapter) == {
        'decision': 'force_ask',
        'reason': '[step 3: step-3]\nforce_ask',
    }


def test_rank_change_preserves_replacement_override_semantics() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(1),
        _output({'decision': 'allow', 'permissionOverrides': ['WEAKER']}),
    )
    adapter.consume(
        _step(2),
        _output({'decision': 'ask', 'permissionOverrides': ['B', 'A']}),
    )

    response_bytes = adapter.finalize().stdout
    response_text = response_bytes.decode('utf-8')
    _response_object, response_end = json.JSONDecoder().raw_decode(response_text)
    assert response_text[response_end:] == '\n'
    assert load_object(response_bytes) == {
        'decision': 'ask',
        'permissionOverrides': ['B', 'A'],
    }


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_rejects_absent_step_idx_distinct_from_present_null(
    event: str,
) -> None:
    payload = load_object(_fixture(event))
    del payload['stepIdx']
    raw_input = f'{json.dumps(payload)}\n'.encode()

    with pytest.raises(HostInputError, match='integer stepIdx'):
        _adapter(event, raw_input)


@pytest.mark.parametrize(
    ('path', 'value'),
    [
        (('toolCall',), None),
        (('toolCall',), False),
        (('toolCall',), 1),
        (('toolCall',), 'call'),
        (('toolCall',), []),
        (('toolCall', 'name'), None),
        (('toolCall', 'name'), False),
        (('toolCall', 'name'), 1),
        (('toolCall', 'name'), ''),
        (('toolCall', 'name'), []),
        (('toolCall', 'name'), {}),
        (('toolCall', 'args'), None),
        (('toolCall', 'args'), False),
        (('toolCall', 'args'), 1),
        (('toolCall', 'args'), 'args'),
        (('toolCall', 'args'), []),
    ],
)
def test_rejects_complete_wrong_type_families_for_pre_tool_fields(
    path: tuple[str, ...],
    value: JsonValue,
) -> None:
    payload = load_object(_fixture('PreToolUse'))
    if len(path) == 1:
        payload[path[0]] = value
    else:
        tool_call = cast('dict[str, object]', payload['toolCall'])
        tool_call[path[1]] = value
    raw_input = f'{json.dumps(payload)}\n'.encode()

    with pytest.raises(HostInputError, match='Antigravity PreToolUse'):
        _adapter('PreToolUse', raw_input)


@pytest.mark.parametrize(
    'missing_path',
    [('toolCall',), ('toolCall', 'name'), ('toolCall', 'args')],
)
def test_rejects_each_missing_pre_tool_field(
    missing_path: tuple[str, ...],
) -> None:
    payload = load_object(_fixture('PreToolUse'))
    if len(missing_path) == 1:
        del payload[missing_path[0]]
    else:
        tool_call = cast('dict[str, JsonValue]', payload['toolCall'])
        del tool_call[missing_path[1]]
    raw_input = f'{json.dumps(payload)}\n'.encode()

    with pytest.raises(HostInputError, match='Antigravity PreToolUse'):
        _adapter('PreToolUse', raw_input)


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
@pytest.mark.parametrize('value', [True, False, 1.5, '1', None, {}, []])
def test_rejects_invalid_step_idx(event: str, value: JsonValue) -> None:
    payload = load_object(_fixture(event))
    payload['stepIdx'] = value
    raw_input = f'{json.dumps(payload)}\n'.encode()

    with pytest.raises(HostInputError, match='stepIdx'):
        _adapter(event, raw_input)


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_repeated_uncontained_terminations_warn_each_time_but_store_once(
    event: str,
) -> None:
    adapter, stream = _adapter(event, allow_on_failure=True)
    if event == 'PreToolUse':
        adapter.consume(
            _step(1),
            _output({'decision': 'ask', 'reason': 'NATIVE_REASON'}),
        )
    for index, failure in enumerate(
        (FailureKind.TIMEOUT, FailureKind.OUTPUT_LIMIT),
        2,
    ):
        control = adapter.consume(
            _step(index),
            StepResult(
                None,
                b'',
                '',
                StepFailure(failure, f'{failure.value} detail'),
                ContainmentState.UNCONTAINED,
            ),
        )
        assert control == 'continue'

    assert stream.getvalue().count(_UNCONTAINED_WARNING) == 2
    response = load_object(adapter.finalize().stdout)
    if event == 'PreToolUse':
        assert cast('str', response['reason']).count(_UNCONTAINED_WARNING) == 1
    else:
        assert response == {}


@pytest.mark.parametrize('decision', ['ask', 'force_ask', 'deny'])
def test_uncontained_reasonless_decision_does_not_create_reason_owner(
    decision: str,
) -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(
        _step(1),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
            ContainmentState.UNCONTAINED,
        ),
    )

    adapter.consume(_step(2), _output({'decision': decision}))

    assert _response(adapter) == {'decision': decision}
    assert stream.getvalue().count(_UNCONTAINED_WARNING) == 1


@pytest.mark.parametrize(
    'failure',
    [
        FailureKind.TIMEOUT,
        FailureKind.OUTPUT_LIMIT,
        FailureKind.OUTPUT_WORKER_TIMEOUT,
    ],
)
def test_uncontained_termination_warns_once_on_developer_stderr(
    failure: FailureKind,
) -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)

    control = adapter.consume(
        _step(1),
        StepResult(
            None,
            b'',
            '',
            StepFailure(failure, f'{failure.value} detail'),
            ContainmentState.UNCONTAINED,
        ),
    )

    assert control == 'continue'
    assert stream.getvalue().count(_UNCONTAINED_WARNING) == 1


@pytest.mark.parametrize('decision', ['ask', 'force_ask', 'deny'])
def test_uncontained_warning_joins_owned_native_reason(decision: str) -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(
        _step(1),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'timeout detail'),
            ContainmentState.UNCONTAINED,
        ),
    )

    adapter.consume(
        _step(2),
        _output({'decision': decision, 'reason': 'NATIVE_REASON'}),
    )

    response = _response(adapter)
    assert response['decision'] == decision
    assert response['reason'] == (
        '[step 2: step-2]\nNATIVE_REASON\n\n'
        f'[step 1: step-1]\n{_UNCONTAINED_WARNING}\n\n'
        '[step 1: step-1]\ntimeout: timeout detail'
    )
    assert stream.getvalue().count(_UNCONTAINED_WARNING) == 1


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_undeliverable_mutation_is_non_relaxable(
    allow_on_failure: bool,
) -> None:
    adapter, stream = _output_failure_adapter(
        'PreToolUse',
        allow_on_failure=allow_on_failure,
    )

    control = adapter.consume_output_failure(
        _step(6),
        OutputFailureKind.UNDELIVERABLE_MUTATION,
    )

    assert control == 'stop'
    assert adapter.finalize().stdout == (
        b'{"decision":"deny","reason":"[step 6: step-6]\\n'
        b'undeliverable mutation: undeliverable_mutation detail"}\n'
    )
    assert stream.getvalue() == (
        'sequential-hooks: step 6 (step-6): undeliverable mutation: '
        'undeliverable_mutation detail\n'
    )


def test_unencodable_stronger_contribution_rolls_back_before_override() -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'reason': 'PRIOR_REASON'}),
    )

    control = adapter.consume(
        _step(2),
        StepResult(0, b'{"decision":"force_ask","reason":"\\ud800"}\n', ''),
    )

    assert control == 'continue'
    assert adapter.finalize().stdout == (
        b'{"decision":"ask","reason":"[step 1: step-1]\\nPRIOR_REASON'
        b'\\n\\n[step 2: step-2]\\nmalformed: '
        b'structured response is not valid UTF-8 text"}\n'
    )
    assert stream.getvalue() == (
        'sequential-hooks: step 2 (step-2): malformed: '
        'structured response is not valid UTF-8 text\n'
    )


def test_unique_or_identical_winning_overrides_preserve_order() -> None:
    adapter, _stream = _adapter('PreToolUse')
    expected: list[JsonValue] = ['second', 'first']
    adapter.consume(
        _step(1),
        _output({'decision': 'ask', 'permissionOverrides': expected}),
    )
    adapter.consume(
        _step(2),
        _output({'decision': 'ask', 'permissionOverrides': expected}),
    )
    adapter.consume(
        _step(3),
        _output({'decision': 'allow', 'permissionOverrides': ['ignored']}),
    )

    assert adapter.finalize().stdout == (
        b'{"decision":"ask","permissionOverrides":["second","first"]}\n'
    )


def test_unknown_output_fields_are_private_and_bounded() -> None:
    sentinel = 'SECRET_SENTINEL_' + ('x' * 200_000)
    adapter, stream = _adapter('PreToolUse')

    control = adapter.consume(
        _step(7),
        _output({'decision': 'allow', sentinel: 'SECRET_VALUE'}),
    )

    assert control == 'stop'
    assert adapter.finalize().stdout == (
        b'{"decision":"deny","reason":"[step 7: step-7]\\n'
        b'unsupported: Antigravity output has 1 unsupported field"}\n'
    )
    diagnostic = stream.getvalue()
    assert diagnostic == (
        'sequential-hooks: step 7 (step-7): unsupported: '
        'Antigravity output has 1 unsupported field\n'
    )
    assert sentinel not in diagnostic
    assert 'SECRET_VALUE' not in diagnostic
    assert sentinel not in adapter.finalize().stdout.decode()
    assert len(diagnostic.encode()) < 200


@pytest.mark.parametrize('decision', ['ask', 'force_ask', 'deny'])
def test_warning_before_later_decision_attaches_only_to_existing_reason(
    decision: str,
) -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(
        _step(1),
        StepResult(None, b'', '', StepFailure(FailureKind.TIMEOUT, 'WARNING_SENTINEL')),
    )

    adapter.consume(
        _step(2),
        _output({'decision': decision, 'reason': 'NATIVE_REASON'}),
    )

    expected = _encoded(
        {
            'decision': decision,
            'reason': (
                '[step 2: step-2]\nNATIVE_REASON\n\n[step 1: step-1]\ntimeout: WARNING_SENTINEL'
            ),
        }
    )
    actual = adapter.finalize().stdout
    assert actual == expected
    assert stream.getvalue() == ('sequential-hooks: step 1 (step-1): timeout: WARNING_SENTINEL\n')


def test_warning_before_reasonless_deny_stays_developer_only() -> None:
    adapter, stream = _adapter('PreToolUse', allow_on_failure=True)
    warning_control = adapter.consume(
        _step(1),
        StepResult(
            None,
            b'',
            '',
            StepFailure(FailureKind.TIMEOUT, 'DEVELOPER_ONLY_WARNING'),
        ),
    )

    deny_control = adapter.consume(
        _step(2),
        _output({'decision': 'deny'}),
    )

    assert warning_control == 'continue'
    assert deny_control == 'stop'
    assert adapter.finalize().stdout == b'{"decision":"deny"}\n'
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (step-1): timeout: DEVELOPER_ONLY_WARNING\n'
    )


@pytest.mark.parametrize('decision', ['allow', 'ask', 'force_ask'])
def test_warning_does_not_attach_to_decision_without_reason(
    decision: str,
) -> None:
    adapter, _stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(_step(1), _output({'decision': decision}))
    adapter.consume(
        _step(2),
        StepResult(None, b'', '', StepFailure(FailureKind.TIMEOUT, 'PRIVATE_WARNING')),
    )

    assert adapter.finalize().stdout == f'{{"decision":"{decision}"}}\n'.encode()


def test_warning_survives_later_rank_change_but_weaker_reason_does_not() -> None:
    adapter, _stream = _adapter('PreToolUse', allow_on_failure=True)
    adapter.consume(_step(1), _output({'decision': 'ask', 'reason': 'WEAKER_REASON'}))
    adapter.consume(
        _step(2),
        StepResult(None, b'', '', StepFailure(FailureKind.TIMEOUT, 'WARNING_SENTINEL')),
    )
    adapter.consume(
        _step(3),
        _output({'decision': 'force_ask', 'reason': 'WINNING_REASON'}),
    )

    assert adapter.finalize().stdout == (
        b'{"decision":"force_ask","reason":"[step 3: step-3]\\nWINNING_REASON'
        b'\\n\\n[step 2: step-2]\\ntimeout: WARNING_SENTINEL"}\n'
    )


def test_weaker_override_after_rank_change_does_not_leak() -> None:
    adapter, _stream = _adapter('PreToolUse')
    adapter.consume(
        _step(1),
        _output({'decision': 'force_ask', 'permissionOverrides': ['WINNER']}),
    )
    adapter.consume(
        _step(2),
        _output({'decision': 'ask', 'permissionOverrides': ['SECRET_WEAKER']}),
    )

    assert adapter.finalize().stdout == (
        b'{"decision":"force_ask","permissionOverrides":["WINNER"]}\n'
    )


@pytest.mark.parametrize('stdout', [b'', b'\n', b' \t\r\n'])
def test_whitespace_only_stdout_is_neutral(stdout: bytes) -> None:
    adapter, stream = _adapter('PreToolUse')

    control = adapter.consume(_step(1), StepResult(0, stdout, ''))

    assert control == 'continue'
    assert adapter.finalize().stdout == b'{"decision":"allow"}\n'
    assert stream.getvalue() == ''


@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
def test_configuration_response_is_independent_of_adapter_construction(
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('configuration response must not construct an adapter')

    monkeypatch.setattr('plugins.agy._sequential_hooks._adapter.AgyAdapter', unavailable)
    monkeypatch.setattr(AgyHost, 'adapter', unavailable)
    stream = io.StringIO()
    response = AgyHost().configuration_response(
        event, 'invalid configuration', Diagnostics(stream)
    )
    message = '[step 1: configuration]\nspawn: sequential-hooks could not safely run this hook'
    expected = {'decision': 'deny', 'reason': message} if event == 'PreToolUse' else {}

    assert response.exit_code == 0
    assert json.loads(response.stdout) == expected
    assert response.stdout.endswith(b'\n')
    assert response.stdout.count(b'\n') == 1
    assert 'sequential-hooks could not safely run this hook' in stream.getvalue()
