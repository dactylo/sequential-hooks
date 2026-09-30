import pytest

from sequential_hooks._types import FailureKind, Step
from sequential_hooks.hosts import (
    CONTAINMENT_STATES,
    FAILURE_KINDS,
    TRUNCATION_MARKER,
    AdapterOutputError,
    OutputFailureKind,
    bounded_contributions,
    continue_after_failure,
    encode_plain_response,
    encode_response,
    labeled,
)


def _step(index: int, name: str) -> Step:
    return Step(index, (name,))


def test_bounded_contributions_accepts_exact_budget_without_truncation() -> None:
    contribution = '[step 1: one]\nbody'

    assert bounded_contributions([contribution], len(contribution)) == contribution


def test_bounded_contributions_rejects_required_labels_over_budget() -> None:
    contributions = [
        f'[step 1: first]\n{"x" * 100}',
        f'[step 2: second]\n{"y" * 100}',
    ]
    labels_with_marker = '[step 1: first]\n\n[step 2: second]' + TRUNCATION_MARKER

    with pytest.raises(AdapterOutputError) as raised:
        bounded_contributions(contributions, len(labels_with_marker) - 1)

    assert raised.value.kind is OutputFailureKind.OVERSIZED


def test_bounded_contributions_retains_multiple_labels_in_order() -> None:
    contributions = [
        '[step 1: first]\none',
        '[step 2: second]\ntwo',
    ]
    expected = '\n\n'.join(contributions)

    assert bounded_contributions(contributions, len(expected)) == expected


def test_bounded_contributions_truncates_final_body_with_marker() -> None:
    label = '[step 1: one]'
    limit = len(label) + len('\nabc') + len(TRUNCATION_MARKER)

    assert bounded_contributions([f'{label}\n{"x" * 100}'], limit) == (
        f'{label}\nxxx{TRUNCATION_MARKER}'
    )


def test_encode_plain_response_rejects_invalid_utf8_surrogate() -> None:
    with pytest.raises(AdapterOutputError) as raised:
        encode_plain_response('\ud800')

    assert raised.value.kind is OutputFailureKind.MALFORMED


@pytest.mark.parametrize('failure', ['new_infrastructure_category', 'new_output_category', ''])
def test_failure_policy_fails_closed_for_unknown_categories(failure: str) -> None:
    with pytest.raises(TypeError, match='failure policy'):
        continue_after_failure(pre_tool=True, failure=failure, allow_on_failure=True)


@pytest.mark.parametrize('pre_tool', [True, False])
@pytest.mark.parametrize('allow_on_failure', [False, True])
@pytest.mark.parametrize('failure', [*FailureKind, *OutputFailureKind])
def test_failure_policy_matches_authoritative_matrix(
    pre_tool: bool,
    allow_on_failure: bool,
    failure: FailureKind | OutputFailureKind,
) -> None:
    actual = continue_after_failure(pre_tool, failure.value, allow_on_failure=allow_on_failure)

    expected = not pre_tool or (
        allow_on_failure and failure is not OutputFailureKind.UNDELIVERABLE_MUTATION
    )
    assert actual is expected


def test_labeled_contribution_is_stable() -> None:
    assert labeled(_step(2, '/opt/tools/example-hook'), 'child text') == (
        '[step 2: example-hook]\nchild text'
    )


def test_published_value_domains_match_the_runtime_enums() -> None:
    assert frozenset(kind.value for kind in FailureKind) == FAILURE_KINDS
    assert frozenset({'contained', 'uncontained'}) == CONTAINMENT_STATES


def test_response_encoders_use_native_framing() -> None:
    assert encode_response({'message': 'snowman \N{SNOWMAN}'}) == (
        '{"message":"snowman \N{SNOWMAN}"}\n'.encode()
    )
    assert encode_plain_response('one line') == b'one line\n'
    assert encode_plain_response('one line\n') == b'one line\n'
    assert encode_plain_response('one line\r') == b'one line\n'
    assert encode_plain_response('one line\r\n') == b'one line\n'


def test_structured_response_encoder_classifies_lone_surrogate() -> None:
    with pytest.raises(AdapterOutputError) as raised:
        encode_response({'message': '\ud800'})

    assert raised.value.kind is OutputFailureKind.MALFORMED
