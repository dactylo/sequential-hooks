"""Compose ordered child results into one native response for any host."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NoReturn

from sequential_hooks.hosts._contract import (
    FAILURE_KINDS,
    ChainControl,
    Diagnostics,
    FinalResponse,
    HostInputError,
    JsonObject,
    JsonValue,
    Step,
    StepResult,
)
from sequential_hooks.hosts._json import JsonProtocolError, dump_object, load_object

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

DIAGNOSTIC_LIMIT_CHARACTERS = 1_000
_DIAGNOSTIC_TRUNCATION_MARKER = '...[truncated]'
FINAL_RESPONSE_LIMIT_BYTES = 1024 * 1024
_GENERIC_TERMINAL_ATTRIBUTION = (
    'native feedback attribution omitted because its step label is too large'
)
_MINIMUM_DIAGNOSTIC_MESSAGE_CHARACTERS = 200
TERMINATION_FAILURES = frozenset({'output_limit', 'output_worker_timeout', 'timeout'})
TRUNCATION_MARKER = '\n[truncated by sequential-hooks]'
_UNCONTAINED_DEDUPLICATION_KEY = 'uncontained-termination'
UNCONTAINED_WARNING = 'step ran without containment; descendant cleanup is not guaranteed'
_INFRASTRUCTURE_FAILURES = FAILURE_KINDS
type OutcomeKind = Literal[
    'developer',
    'failure',
    'native_exit',
    'neutral',
    'ordinary_exit',
    'plain',
    'structured',
]


class OutputFailureKind(StrEnum):
    """Classify rejected child or final adapter output.

    Attributes:
        MALFORMED: Output with invalid native syntax, types, or required shape.
        UNSUPPORTED: Well-formed output outside the supported host contract.
        OVERSIZED: Output that cannot fit its documented size budget.
        UNDELIVERABLE_MUTATION: Replacement candidate that cannot be delivered
            exactly. On pre-tool events, `--allow-on-failure` cannot override
            this category.
    """

    MALFORMED = 'malformed'
    UNSUPPORTED = 'unsupported'
    OVERSIZED = 'oversized'
    UNDELIVERABLE_MUTATION = 'undeliverable_mutation'


class AdapterOutputError(ValueError):
    """Report a classified adapter output rejection.

    Attributes:
        kind: Stable adapter output failure category.
    """

    kind: OutputFailureKind

    def __init__(self, kind: OutputFailureKind, message: str) -> None:
        """Retain the failure category and rejection detail.

        Args:
            kind: Stable malformed, unsupported, oversized, or undeliverable-
                mutation category.
            message: Rejection detail.
        """
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Contribution:
    """Carry one validated structured contribution from a child step.

    Every field is optional; a host's `parse_structured` fills the fields its
    protocol defines and leaves the rest at their defaults.

    Attributes:
        additional_context: Model-facing context to append.
        continue_value: Explicit global continue flag, when present.
        decision: Host-native decision value, when present.
        decision_reason: Reason attached to `decision`, when present.
        mutation: Replacement value for the event's mutation field.
        mutation_present: Whether the contribution carries a mutation.
        stop_reason: Reason attached to `continue_value` false.
        suppress_output: Explicit output suppression flag, when present.
        system_message: User-facing message to append.
        terminal_sequence: Terminal control sequence, when present.
    """

    additional_context: str | None = None
    continue_value: bool | None = None
    decision: str | None = None
    decision_reason: str | None = None
    mutation: JsonValue = None
    mutation_present: bool = False
    stop_reason: str | None = None
    suppress_output: bool | None = None
    system_message: str | None = None
    terminal_sequence: str | None = None


@dataclass(frozen=True)
class Outcome:
    """Carry one classified child result before it changes adapter state.

    Attributes:
        kind: Classification the composer dispatches on.
        developer_text: Text for the developer tier when `kind` is `developer`.
        failure: Failure category when `kind` is `failure`.
        message: Failure detail or native exit feedback.
        plain: Native plain text when `kind` is `plain`.
        returncode: Child status when `kind` is `ordinary_exit`.
        structured: Validated contribution when `kind` is `structured`.
        uncontained: Whether the failed step ran without containment.
    """

    kind: OutcomeKind
    developer_text: str = ''
    failure: str | None = None
    message: str = ''
    plain: str = ''
    returncode: int | None = None
    structured: Contribution | None = None
    uncontained: bool = False


@dataclass(frozen=True)
class NativeResponse:
    """Carry one final native response and its wrapper exit status.

    Attributes:
        stdout: Exact native response bytes.
        exit_code: Wrapper process exit status.
    """

    stdout: bytes
    exit_code: int = 0


@dataclass(frozen=True)
class UserContribution:
    """Carry one ordered user-tier contribution and its shedding ownership.

    Attributes:
        mandatory: Whether the message survives message shedding.
        message: Labeled text emitted at full size.
        deduplication_key: Key that suppresses a repeated mandatory report.
        minimal_message: Unlabeled text emitted under the minimal-report stage;
            `None` for a non-mandatory message.
    """

    mandatory: bool
    message: str
    deduplication_key: str | None = None
    minimal_message: str | None = None


@dataclass
class CompositionState:
    """Declare every versioned composition value the rollback snapshot covers.

    The snapshot shallow-copies every list, set, and dict value; every other
    value is replaced, never mutated, so the reference itself is the version.

    Attributes:
        contexts: Labeled model-facing contexts in step order.
        continue_false: Whether a step committed a global stop.
        decision: Winning host-native decision, when any.
        decision_reasons: Labeled reasons at the winning rank.
        decision_report: Mandatory report that owns a failure-caused deny.
        messages: User-tier contributions in acceptance order.
        minimal_reports: Whether mandatory reports emit their minimal text.
        payload: Current native payload after accepted mutations.
        plain: Native plain contributions awaiting finalization.
        plain_dropped_reported: Step indexes whose dropped plain output was
            already diagnosed.
        raw_input: Current native input bytes for the next child.
        response_bytes: Last structured response that fit, or `None` before any
            structured result.
        shed_contexts: Whether contexts are shed from the current response.
        shed_messages: Whether non-mandatory messages are shed.
        stop_reason: Labeled reason attached to the global stop.
        stop_reason_limit: Character budget applied to `stop_reason`.
        structured_seen: Whether any structured or failed result was applied.
        suppress_output: Sticky output suppression flag, when set.
        terminal_sequence: First accepted terminal sequence.
        terminal_sequence_owner: Contribution that supplied it.
        updated_input: Accepted pre-tool mutation.
        updated_input_present: Whether `updated_input` was accepted.
        updated_output: Accepted post-tool mutation.
        updated_output_present: Whether `updated_output` was accepted.
        winning_reason_limit: Character budget applied to `decision_reasons`.
    """

    payload: JsonObject
    raw_input: bytes
    contexts: list[str] = field(default_factory=list[str])
    continue_false: bool = False
    decision: str | None = None
    decision_reasons: list[str] = field(default_factory=list[str])
    decision_report: UserContribution | None = None
    messages: list[UserContribution] = field(default_factory=list[UserContribution])
    minimal_reports: bool = False
    plain: list[tuple[Step, str]] = field(default_factory=list[tuple[Step, str]])
    plain_dropped_reported: set[int] = field(default_factory=set[int])
    response_bytes: bytes | None = None
    shed_contexts: bool = False
    shed_messages: bool = False
    stop_reason: str | None = None
    stop_reason_limit: int | None = None
    structured_seen: bool = False
    suppress_output: bool | None = None
    terminal_sequence: str | None = None
    terminal_sequence_owner: Contribution | None = None
    updated_input: JsonValue = None
    updated_input_present: bool = False
    updated_output: JsonValue = None
    updated_output_present: bool = False
    winning_reason_limit: int | None = None


class Composer:
    """Compose ordered child results for one validated native invocation.

    Subclass it per host and override the protocol surfaces. The runner sees
    only the `HostAdapter` interface. Subclasses set `pre_tool` and any
    event-dependent `mutation_field` before calling `super().__init__`;
    fixed protocol facts override the class defaults below.

    Attributes:
        allow_on_failure: Whether approved pre-tool failures may continue.
        blocking_exit: Child exit status that carries terminal native feedback.
        diagnostics: Developer-tier diagnostic sink.
        field_limit: Character cap the host documents for composed text fields,
            or `None` when only the final byte limit applies.
        mutation_field: Payload field an accepted mutation replaces, or `None`
            when the event has no mutation.
        pre_tool: Whether the event decides a proposed tool call.
        state: Versioned composition state covered by the rollback snapshot.
    """

    blocking_exit = 2
    field_limit: int | None = None
    mutation_field: str | None = None
    pre_tool: bool

    def __init__(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> None:
        """Bind the host facts and take ownership of the native payload.

        Args:
            event: Trusted native event.
            raw_input: Original strict JSON bytes received from the host.
            payload: Decoded strict JSON object matching `raw_input`.
            diagnostics: Developer-tier diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Raises:
            HostInputError: If copying the payload exceeds the recursion limit,
                which means the input nests too deeply to compose.
        """
        try:
            owned_payload = deepcopy(payload)
        except RecursionError as error:
            raise HostInputError('native input nests too deeply') from error
        self.allow_on_failure = allow_on_failure
        self.diagnostics = diagnostics
        self.state = CompositionState(owned_payload, raw_input)
        self._event = event

    def _add_mandatory_report(
        self,
        step: Step,
        message: str,
        *,
        deduplication_key: str | None = None,
        minimal_message: str | None = None,
    ) -> tuple[UserContribution, bool]:
        """Record one mandatory report in global acceptance order."""
        if deduplication_key is not None:
            for contribution in self.state.messages:
                if contribution.deduplication_key == deduplication_key:
                    return contribution, False
        report = UserContribution(
            mandatory=True,
            message=labeled(step, message),
            deduplication_key=deduplication_key,
            minimal_message=message if minimal_message is None else minimal_message,
        )
        self.state.messages.append(report)
        return report, True

    def _apply_mutation(self, value: Contribution) -> None:
        """Replace only the event-owned input field for an accepted mutation."""
        if not value.mutation_present:
            return
        if self.mutation_field is None:
            raise RuntimeError('validated mutation has no mutation field')
        mutation = deepcopy(value.mutation)
        candidate_payload = dict(self.state.payload)
        candidate_payload[self.mutation_field] = mutation
        try:
            candidate_input = dump_object(candidate_payload)
        except (JsonProtocolError, UnicodeEncodeError) as error:
            raise AdapterOutputError(
                OutputFailureKind.MALFORMED,
                'mutated native input is not valid UTF-8 text',
            ) from error
        self.state.payload = candidate_payload
        self.state.raw_input = candidate_input
        if self.pre_tool:
            self.state.updated_input = mutation
            self.state.updated_input_present = True
        else:
            self.state.updated_output = mutation
            self.state.updated_output_present = True

    def _plain_discard_diagnostic(
        self,
        text: str,
        *,
        reason: str,
    ) -> str:
        """Return one bounded diagnostic containing discarded plain output."""
        return bounded_diagnostic(f'plain stdout omitted because {reason}: ', text)

    def _replace_terminal_attribution(
        self,
        owner: Contribution | None,
        terminal_reason: str | None,
    ) -> None:
        """Replace an unrepresentable label while retaining its bounded reason."""
        state = self.state
        if state.decision in ('deny', 'block'):
            reason = terminal_reason
            if reason is None and owner is not None:
                reason = owner.decision_reason
            state.decision_reasons = [reason] if reason is not None else []
            state.decision_report = None
        if state.continue_false:
            state.stop_reason = owner.stop_reason if owner is not None else None

    def _replace_terminal_attribution_with_generic(self) -> None:
        """Use minimal terminal feedback when even the unlabeled reason cannot fit."""
        state = self.state
        if state.decision in ('deny', 'block'):
            state.decision_reasons = [_GENERIC_TERMINAL_ATTRIBUTION]
            state.decision_report = None
        if state.continue_false:
            state.stop_reason = None

    def _restore(self, snapshot: dict[str, object]) -> None:
        """Restore composition state after a rejected candidate publication."""
        for name, value in snapshot.items():
            setattr(self.state, name, value)

    def _snapshot(self) -> dict[str, object]:
        """Shallow-copy every list, set, and dict value of the declared state."""
        snapshot: dict[str, object] = {}
        for name, value in vars(self.state).items():
            snapshot[name] = value.copy() if isinstance(value, (list, set, dict)) else value
        return snapshot

    def apply_decision(self, step: Step, value: Contribution) -> None:
        """Compose one native decision under the host's precedence rule.

        The default accepts no decision; a host with decisions overrides it.

        Args:
            step: Child that supplied `value`.
            value: Validated contribution whose `decision` may be set.
        """
        del step
        if value.decision is not None:
            raise RuntimeError('host accepted a decision it does not compose')

    def _discard_plain(
        self,
        pending_diagnostics: list[tuple[Step, str]] | None = None,
        *,
        reason: str = 'structured output survived',
    ) -> None:
        """Discard pending plain output and diagnose each discarded text."""
        state = self.state
        if not state.plain:
            return
        for plain_step, text in state.plain:
            diagnostic = self._plain_discard_diagnostic(text, reason=reason)
            if pending_diagnostics is None:
                self.write_diagnostic(plain_step, diagnostic)
            else:
                pending_diagnostics.append((plain_step, diagnostic))
        state.plain.clear()

    def bounded_contexts(self) -> str | None:
        """Return the model-facing contexts after shedding, if any.

        Returns:
            Bounded labeled contexts, or `None` when shed or absent.
        """
        state = self.state
        if not state.contexts or state.shed_contexts:
            return None
        return self.bounded(state.contexts)

    def bounded_decision_reasons(self) -> str | None:
        """Return the winning decision reasons after shedding, if any.

        Returns:
            Bounded labeled reasons, the unlabeled minimal report under the
            minimal-report stage, or `None` when no reason survives.
        """
        state = self.state
        reasons = state.decision_reasons
        if state.minimal_reports and state.decision_report is not None:
            minimal = state.decision_report.minimal_message
            reasons = [minimal] if minimal is not None else []
        if not reasons:
            return None
        return self.bounded(reasons, state.winning_reason_limit)

    def bounded_messages(self) -> str | None:
        """Return the user-tier messages after shedding, if any.

        Returns:
            Bounded messages in acceptance order, or `None` when none survive.
        """
        state = self.state
        messages: list[str] = []
        for contribution in state.messages:
            if not contribution.mandatory and state.shed_messages:
                continue
            if state.minimal_reports and contribution.mandatory:
                if contribution.minimal_message is None:
                    raise RuntimeError('mandatory contribution omits minimal text')
                messages.append(contribution.minimal_message)
            else:
                messages.append(contribution.message)
        if not messages:
            return None
        return self.bounded(messages)

    def bounded_stop_reason(self) -> str | None:
        """Return the global stop reason after shedding, if any.

        Returns:
            Bounded labeled stop reason, or `None`.
        """
        state = self.state
        if state.stop_reason is None:
            return None
        return self.bounded([state.stop_reason], state.stop_reason_limit)

    def build_response(self) -> JsonObject:
        """Build the current structured native response.

        A host overrides this with its response shape, reading the bounded
        accessors and `state`. An empty object means no structured response.

        Returns:
            Native response object before encoding.
        """
        return {}

    def _response_candidate(self) -> bytes | None:
        """Build and encode one current structured response candidate."""
        # A label that cannot fit its host field is a rejected candidate and
        # propagates; only an oversized encoding is a no-fit.
        response = self.build_response()
        if not response:
            return b''
        try:
            return encode_response(response)
        except AdapterOutputError as error:
            if error.kind is OutputFailureKind.OVERSIZED:
                return None
            raise

    def _response_fits(self) -> bool:
        """Encode and retain the current response when it fits."""
        response = self._response_candidate()
        if response is None:
            return False
        self.state.response_bytes = response
        return True

    def _fit_labeled_field(
        self,
        contributions: list[str],
        field_name: Literal['stop_reason_limit', 'winning_reason_limit'],
    ) -> bool:
        """Fit one labeled field by binary search while retaining every label."""
        if not contributions:
            return False
        labels = [contribution.partition('\n')[0] for contribution in contributions]
        low = len('\n\n'.join(labels)) + len(TRUNCATION_MARKER)
        joined = len('\n\n'.join(contributions))
        high = joined if self.field_limit is None else min(self.field_limit, joined)
        best: int | None = None
        while low <= high:
            middle = (low + high) // 2
            setattr(self.state, field_name, middle)
            if self._response_fits():
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        setattr(self.state, field_name, low if best is None else best)
        return best is not None

    def classify_unparsable(self, text: str, stripped: bytes, error: str) -> Outcome:
        """Classify exit-zero stdout that is not one strict JSON object.

        The default treats every such text as malformed. Hosts with a plain or
        developer text tier override it.

        Args:
            text: Decoded stdout.
            stripped: Stdout without leading ASCII whitespace.
            error: Parser detail for the malformed classification.

        Returns:
            Plain, developer, or failure outcome.
        """
        del text, stripped
        return Outcome('failure', failure=OutputFailureKind.MALFORMED.value, message=error)

    def fit_response(
        self,
        owner: Contribution | None = None,
        *,
        minimal_reports: bool = False,
    ) -> None:
        """Shed monotonically until the structured response fits.

        The stages run in a fixed order: contexts, non-mandatory messages,
        minimal reports, the winning-reason field, then the stop-reason field.
        A field shed to its label-only minimum stays there.

        Args:
            owner: Contribution being published, when the fit follows one.
            minimal_reports: Whether to start with mandatory reports already
                reduced to their minimal text.

        Raises:
            AdapterOutputError: If no stage fits; `UNDELIVERABLE_MUTATION` when
                `undeliverable_owner(owner)` holds, else `OVERSIZED`.
        """
        state = self.state
        state.minimal_reports = minimal_reports
        state.shed_contexts = False
        state.shed_messages = False
        state.stop_reason_limit = None
        state.winning_reason_limit = None
        if self._response_fits():
            return
        state.shed_contexts = True
        if self._response_fits():
            return
        state.shed_messages = True
        if self._response_fits():
            return
        if not state.minimal_reports:
            state.minimal_reports = True
            if self._response_fits():
                return
        if self._fit_labeled_field(state.decision_reasons, 'winning_reason_limit'):
            return
        stop_reasons = [state.stop_reason] if state.stop_reason is not None else []
        if self._fit_labeled_field(stop_reasons, 'stop_reason_limit'):
            return
        kind = (
            OutputFailureKind.UNDELIVERABLE_MUTATION
            if owner is not None and self.undeliverable_owner(owner)
            else OutputFailureKind.OVERSIZED
        )
        raise AdapterOutputError(
            kind,
            (
                'mandatory decision-bearing output cannot be delivered within '
                'the final response limit'
                if kind is OutputFailureKind.UNDELIVERABLE_MUTATION
                else 'structured contribution exceeds the final response limit'
            ),
        )

    def failure_texts(self, category: str, message: str) -> tuple[str, str, str]:
        """Return the developer diagnostic, user report, and minimal report for a failure.

        Args:
            category: Failure category with underscores replaced by spaces.
            message: Failure detail, possibly empty.

        Returns:
            Diagnostic text, report text awaiting step attribution, and minimal
            report text emitted without step attribution.
        """
        detail = message or f'{category} child result'
        return (
            f'{category}: {detail}',
            f'{category}: {detail}',
            f'{category}: see developer diagnostics',
        )

    def fits_without_terminal_sequence(self) -> bool:
        """Return whether the current response fits once the terminal sequence is dropped.

        The probe restores the sequence before returning.

        Returns:
            Whether a response without the terminal sequence fits.
        """
        state = self.state
        terminal_sequence = state.terminal_sequence
        state.terminal_sequence = None
        try:
            return self._response_fits()
        finally:
            state.terminal_sequence = terminal_sequence

    def ordinary_exit_texts(self, returncode: int) -> tuple[str, str]:
        """Return the diagnostic and user report for a non-blocking non-zero exit.

        Args:
            returncode: Child status.

        Returns:
            Diagnostic text and report text.
        """
        detail = f'exited with status {returncode}'
        return detail, detail

    def parse_structured(self, value: JsonObject) -> Contribution:
        """Validate one whole structured output object.

        Args:
            value: Strict JSON object a child wrote to stdout.

        Returns:
            Validated contribution.

        Raises:
            AdapterOutputError: If the object is outside the host contract.
        """
        del value
        raise AdapterOutputError(
            OutputFailureKind.UNSUPPORTED,
            'structured output is not supported by this host',
        )

    def classify_stdout(self, stdout: bytes) -> Outcome:
        """Classify exit-zero stdout under the host's native boundary.

        Args:
            stdout: Bounded child stdout.

        Returns:
            Neutral, structured, plain, developer, or failure outcome.
        """
        stripped = stdout.lstrip(b' \t\r\n')
        if not stripped:
            return Outcome('neutral')
        try:
            text = stdout.decode()
        except UnicodeDecodeError:
            return Outcome(
                'failure',
                failure=OutputFailureKind.MALFORMED.value,
                message='stdout is not valid UTF-8',
            )
        try:
            value = load_object(stdout)
        except JsonProtocolError as error:
            return self.classify_unparsable(text, stripped, str(error))
        try:
            dump_object(value)
            structured = self.parse_structured(value)
        except (JsonProtocolError, UnicodeEncodeError):
            return Outcome(
                'failure',
                failure=OutputFailureKind.MALFORMED.value,
                message='structured stdout is not valid UTF-8 text',
            )
        except AdapterOutputError as error:
            return Outcome('failure', failure=error.kind.value, message=str(error))
        return Outcome('structured', structured=structured) if value else Outcome('neutral')

    def _classify(self, result: StepResult) -> Outcome:
        """Classify infrastructure and exit control before stdout."""
        if result.failure is not None:
            return Outcome(
                'failure',
                failure=str(result.failure.kind),
                message=result.failure.message,
                uncontained=str(result.containment) == 'uncontained',
            )
        if result.returncode == self.blocking_exit:
            return Outcome('native_exit', message=result.blocking_stderr)
        if result.returncode not in (None, 0):
            return Outcome('ordinary_exit', returncode=result.returncode)
        return self.classify_stdout(result.stdout)

    def _fit_terminal(
        self,
        step: Step,
        owner: Contribution | None,
        *,
        terminal_reason: str | None = None,
    ) -> None:
        """Fit terminal control, replacing its attribution before giving up."""
        state = self.state
        try:
            self.fit_response(owner)
        except AdapterOutputError as error:
            failure = error
        else:
            return
        state.contexts.clear()
        state.messages = [message for message in state.messages if message.mandatory]
        self._replace_terminal_attribution(owner, terminal_reason)
        try:
            self.fit_response(owner, minimal_reports=True)
        except AdapterOutputError:
            pass
        else:
            return
        self._replace_terminal_attribution_with_generic()
        try:
            self.fit_response(owner, minimal_reports=True)
        except AdapterOutputError:
            pass
        else:
            return
        if owner is None and self.terminal_fallback(step):
            return
        raise failure

    def step_input(self) -> bytes:
        """Return native JSON input for the next child.

        Returns:
            Original input until an accepted mutation changes its owned field.
        """
        return self.state.raw_input

    def terminal_fallback(self, step: Step) -> bool:
        """Apply the host's last-resort stage for a blocking native exit.

        Args:
            step: Child whose exit is being delivered.

        Returns:
            Whether the response now fits.
        """
        del step
        return False

    def undeliverable_owner(self, owner: Contribution) -> bool:
        """Return whether an unfittable owner carries mandatory output.

        Args:
            owner: Contribution whose publication cannot fit.

        Returns:
            `True` when dropping the owner would run an operation without its
            required mutation; the default checks only the mutation.
        """
        return owner.mutation_present

    def _apply_failure(
        self,
        step: Step,
        failure: str,
        message: str,
        *,
        uncontained: bool = False,
    ) -> ChainControl:
        """Publish one matrix failure through both diagnostic tiers."""
        state = self.state
        self._discard_plain(reason='a step failure ended the chain')
        if uncontained and failure in TERMINATION_FAILURES:
            _warning, warning_added = self._add_mandatory_report(
                step,
                UNCONTAINED_WARNING,
                deduplication_key=_UNCONTAINED_DEDUPLICATION_KEY,
            )
            if warning_added:
                self.write_diagnostic(step, UNCONTAINED_WARNING)
        category = failure.replace('_', ' ')
        diagnostic, report_text, minimal = self.failure_texts(category, message)
        self.write_diagnostic(step, diagnostic)
        report, _added = self._add_mandatory_report(step, report_text, minimal_message=minimal)
        state.structured_seen = True
        should_continue = continue_after_failure(
            self.pre_tool,
            failure,
            allow_on_failure=self.allow_on_failure,
        )
        if not should_continue and self.pre_tool:
            state.decision = 'deny'
            state.decision_reasons = [report.message]
            state.decision_report = report
        try:
            self.fit_response()
        except AdapterOutputError:
            # The report's own label may exceed the host field; reduce every
            # mandatory report to its minimal text and keep the matrix decision.
            self.fit_response(minimal_reports=True)
        return 'continue' if should_continue else 'stop'

    def bounded(self, contributions: list[str], limit: int | None = None) -> str:
        """Bound labeled contributions under the host field cap.

        Args:
            contributions: Labeled contributions in step order.
            limit: Explicit character budget from a fitted field, if any.

        Returns:
            Joined contributions, truncated with every label retained.

        Raises:
            AdapterOutputError: If truncation is required and the labels plus
                the truncation marker cannot fit within the budget.
        """
        if limit is None:
            limit = (
                len('\n\n'.join(contributions)) if self.field_limit is None else self.field_limit
            )
        return bounded_contributions(contributions, limit)

    def write_diagnostic(self, step: Step, message: str) -> None:
        """Write one step diagnostic whose complete attributed line is bounded.

        Args:
            step: Child the message belongs to.
            message: Diagnostic text; truncated with a marker when the labeled
                line would exceed `DIAGNOSTIC_LIMIT_CHARACTERS`.
        """
        intended = f'sequential-hooks: step {step.index} ({step.name}): {message}\n'
        if len(intended) <= DIAGNOSTIC_LIMIT_CHARACTERS:
            self.diagnostics.write_step(step, message)
            return
        index = step.index
        fixed_characters = len(f'sequential-hooks: step {index} (): \n')
        if (
            fixed_characters + _MINIMUM_DIAGNOSTIC_MESSAGE_CHARACTERS
            >= DIAGNOSTIC_LIMIT_CHARACTERS
        ):
            index = 0
            fixed_characters = len('sequential-hooks: step 0 (): \n')
        name_budget = max(
            0,
            DIAGNOSTIC_LIMIT_CHARACTERS
            - fixed_characters
            - min(len(message), _MINIMUM_DIAGNOSTIC_MESSAGE_CHARACTERS),
        )
        safe_name = _truncate(step.name, name_budget)
        message_budget = max(0, DIAGNOSTIC_LIMIT_CHARACTERS - fixed_characters - len(safe_name))
        self.diagnostics.write_step(
            _DiagnosticStep(index, (safe_name,)),
            _truncate(message, message_budget),
        )

    @property
    def event(self) -> str:
        """Return the validated native event.

        Returns:
            Trusted event bound at construction.
        """
        return self._event

    @property
    def should_run_steps(self) -> bool:
        """Return whether this invocation should execute child steps.

        Returns:
            `True` for every composed invocation.
        """
        return True

    def _apply_native_exit(self, step: Step, message: str) -> ChainControl:
        """Translate the host's blocking exit into terminal native feedback."""
        state = self.state
        self._discard_plain()
        native_reason = message or f'exited with status {self.blocking_exit}'
        state.structured_seen = True
        state.decision = 'deny' if self.pre_tool else 'block'
        state.decision_reasons = [labeled(step, native_reason)]
        state.decision_report = None
        self._fit_terminal(step, None, terminal_reason=native_reason)
        return 'stop'

    def _apply_ordinary_exit(self, step: Step, returncode: int) -> ChainControl:
        """Report one documented non-blocking child exit."""
        self._discard_plain()
        diagnostic, report = self.ordinary_exit_texts(returncode)
        self.write_diagnostic(step, diagnostic)
        self._add_mandatory_report(step, report)
        self.state.structured_seen = True
        self.fit_response()
        return 'continue'

    def _apply_structured(
        self,
        step: Step,
        value: Contribution,
    ) -> tuple[bool, list[tuple[Step, str]]]:
        """Apply one validated contribution and report whether it is terminal."""
        state = self.state
        pending_diagnostics: list[tuple[Step, str]] = []
        state.structured_seen = True
        self._discard_plain(pending_diagnostics)
        if value.system_message is not None:
            state.messages.append(
                UserContribution(mandatory=False, message=labeled(step, value.system_message))
            )
        if value.suppress_output is not None:
            state.suppress_output = (
                value.suppress_output
                if state.suppress_output is None
                else state.suppress_output or value.suppress_output
            )
        if value.terminal_sequence is not None:
            if state.terminal_sequence is None:
                state.terminal_sequence = value.terminal_sequence
                state.terminal_sequence_owner = value
            else:
                pending_diagnostics.append(
                    (step, 'terminalSequence dropped because an earlier step supplied one')
                )
        if value.additional_context is not None:
            state.contexts.append(labeled(step, value.additional_context))
        self._apply_mutation(value)
        self.apply_decision(step, value)
        if value.continue_value is False:
            state.continue_false = True
            state.stop_reason = (
                labeled(step, value.stop_reason) if value.stop_reason is not None else None
            )
            return True, pending_diagnostics
        return value.decision in ('deny', 'block'), pending_diagnostics

    def _plain_response(self, *, diagnose: bool) -> bytes:
        """Build the longest whole-prefix plain response that fits."""
        state = self.state
        if not state.plain:
            return b''
        if len(state.plain) == 1:
            return encode_plain_response(state.plain[0][1])
        for retained_count in range(len(state.plain), 0, -1):
            joined = '\n\n'.join(
                labeled(step, text) for step, text in state.plain[:retained_count]
            )
            try:
                response = encode_plain_response(joined)
            except AdapterOutputError:
                continue
            if diagnose:
                for dropped_step, text in state.plain[retained_count:]:
                    if dropped_step.index not in state.plain_dropped_reported:
                        self.write_diagnostic(
                            dropped_step,
                            self._plain_discard_diagnostic(
                                text,
                                reason='it exceeds the final response limit',
                            ),
                        )
                        state.plain_dropped_reported.add(dropped_step.index)
            return response
        raise AdapterOutputError(
            OutputFailureKind.OVERSIZED,
            'first labeled plain contribution exceeds final response limit',
        )

    def _apply_plain(self, step: Step, text: str) -> ChainControl:
        """Publish one plain contribution or its classified size failure."""
        state = self.state
        if state.structured_seen:
            self.write_diagnostic(
                step,
                self._plain_discard_diagnostic(text, reason='structured output survived'),
            )
            return 'continue'
        try:
            encode_plain_response(text)
        except AdapterOutputError as error:
            return self._apply_failure(step, error.kind.value, str(error))
        state.plain.append((step, text))
        try:
            self._plain_response(diagnose=False)
        except AdapterOutputError as error:
            state.plain.pop()
            return self._apply_failure(step, error.kind.value, str(error))
        return 'continue'

    def _publish_structured(self, step: Step, value: Contribution) -> ChainControl:
        """Apply one validated structured candidate atomically."""
        snapshot = self._snapshot()
        try:
            terminal, pending_diagnostics = self._apply_structured(step, value)
            if terminal:
                self._fit_terminal(step, value)
            else:
                self.fit_response(value)
        except AdapterOutputError as error:
            self._restore(snapshot)
            return self._apply_failure(step, error.kind.value, str(error))
        for diagnostic_step, diagnostic in pending_diagnostics:
            self.write_diagnostic(diagnostic_step, diagnostic)
        return 'stop' if terminal else 'continue'

    def consume(self, step: Step, result: StepResult) -> ChainControl:
        """Apply one child result and choose whether the runner continues.

        Args:
            step: Child that produced `result`.
            result: Bounded process outcome to interpret.

        Returns:
            `'continue'` or `'stop'`.

        Raises:
            AdapterOutputError: If a failure report cannot be represented even
                after every shedding stage; the wrapper handles this propagated
                error through Tier 3.
        """
        outcome = self._classify(result)
        if outcome.kind in ('neutral', 'developer'):
            if outcome.kind == 'developer':
                self.write_diagnostic(
                    step,
                    bounded_diagnostic('developer stdout omitted: ', outcome.developer_text),
                )
            return 'continue'
        if outcome.kind == 'plain':
            return self._apply_plain(step, outcome.plain)
        if outcome.kind == 'failure':
            if outcome.failure is None:
                raise RuntimeError('classified failure omits its category')
            return self._apply_failure(
                step,
                outcome.failure,
                outcome.message,
                uncontained=outcome.uncontained,
            )
        if outcome.kind == 'native_exit':
            return self._apply_native_exit(step, outcome.message)
        if outcome.kind == 'ordinary_exit':
            if outcome.returncode is None:
                raise RuntimeError('classified exit omits its status')
            return self._apply_ordinary_exit(step, outcome.returncode)
        if outcome.structured is None:
            raise RuntimeError('structured outcome omits its value')
        return self._publish_structured(step, outcome.structured)

    def finalize(self) -> FinalResponse:
        """Return the final native host response.

        Returns:
            The last structured response that fit, otherwise the plain
            response, otherwise empty stdout.
        """
        if self.state.response_bytes is not None:
            return NativeResponse(self.state.response_bytes)
        return NativeResponse(self._plain_response(diagnose=True))


@dataclass(frozen=True)
class _DiagnosticStep:
    """Stand in for a step whose name was truncated for one diagnostic line."""

    index: int
    argv: tuple[str, ...]

    @property
    def name(self) -> str:
        """Return the truncated name."""
        return self.argv[0]


def _truncate(value: str, limit: int) -> str:
    """Truncate one diagnostic component within its exact budget."""
    if len(value) <= limit:
        return value
    if limit <= len(_DIAGNOSTIC_TRUNCATION_MARKER):
        return value[:limit]
    return f'{value[: limit - len(_DIAGNOSTIC_TRUNCATION_MARKER)]}{_DIAGNOSTIC_TRUNCATION_MARKER}'


def bounded_contributions(contributions: Collection[str], limit: int) -> str:
    """Fit labeled contributions into one character budget.

    Args:
        contributions: Contributions in accepted step order, each with its
            label on the first line and its body on subsequent lines.
        limit: Maximum returned character count.

    Returns:
        Contributions joined in input order. When truncation is required, every
        label survives, remaining body space goes to earlier contributions
        first, and `TRUNCATION_MARKER` is appended.

    Raises:
        AdapterOutputError: If truncation is required and the labels plus
            `TRUNCATION_MARKER` cannot fit within the budget.
    """
    joined = '\n\n'.join(contributions)
    if len(joined) <= limit:
        return joined
    labeled_content = [contribution.partition('\n') for contribution in contributions]
    labels_with_marker = (
        '\n\n'.join(label for label, _separator, _content in labeled_content) + TRUNCATION_MARKER
    )
    if len(labels_with_marker) > limit:
        raise AdapterOutputError(
            OutputFailureKind.OVERSIZED,
            'labeled contributions cannot fit within the response field limit',
        )
    remaining = limit - len(labels_with_marker)
    retained: list[str] = []
    for label, _separator, content in labeled_content:
        if content and remaining > 1:
            retained_characters = min(len(content), remaining - 1)
            retained.append(f'{label}\n{content[:retained_characters]}')
            remaining -= retained_characters + 1
        else:
            retained.append(label)
    return '\n\n'.join(retained) + TRUNCATION_MARKER


def bounded_diagnostic(prefix: str, detail: str) -> str:
    """Prefix a repr-quoted detail and truncate it against the diagnostic budget.

    Args:
        prefix: Text retained in full before the quoted detail.
        detail: Text to quote and truncate.

    Returns:
        Prefixed diagnostic, with `...[truncated]` appended when truncation is
        needed. The result fits `DIAGNOSTIC_LIMIT_CHARACTERS` when the prefix
        leaves room for that marker; longer prefixes can exceed the limit.
    """
    representation = repr(detail)
    if len(prefix) + len(representation) <= DIAGNOSTIC_LIMIT_CHARACTERS:
        return f'{prefix}{representation}'
    retained = DIAGNOSTIC_LIMIT_CHARACTERS - len(prefix) - len(_DIAGNOSTIC_TRUNCATION_MARKER)
    return f'{prefix}{representation[:retained]}{_DIAGNOSTIC_TRUNCATION_MARKER}'


def continue_after_failure(
    pre_tool: bool,
    failure: str,
    *,
    allow_on_failure: bool,
) -> bool:
    """Apply the one authoritative failure-override scope.

    Args:
        pre_tool: Whether the event permits a proposed tool call.
        failure: Infrastructure or output failure category.
        allow_on_failure: Whether the explicit pre-tool override is active.

    Returns:
        Whether a later step may run after the failure.

    Raises:
        TypeError: If `failure` is not a matrix category.
    """
    if failure not in _INFRASTRUCTURE_FAILURES and failure not in OutputFailureKind:
        raise TypeError('failure policy requires a matrix condition')
    if not pre_tool:
        return True
    return allow_on_failure and (
        failure in _INFRASTRUCTURE_FAILURES
        or (failure in OutputFailureKind and failure != OutputFailureKind.UNDELIVERABLE_MUTATION)
    )


def encode_plain_response(text: str) -> bytes:
    """Encode one plain-text response within the final byte budget.

    Args:
        text: Native successful plain-text contribution.

    Returns:
        UTF-8 bytes with a trailing line feed. A final CR or CRLF is normalized
        to LF; existing additional trailing line feeds are preserved.

    Raises:
        AdapterOutputError: If the text is not UTF-8 encodable or exceeds the
            final response budget.
    """
    if text.endswith('\r\n'):
        text = f'{text[:-2]}\n'
    elif text.endswith('\r'):
        text = f'{text[:-1]}\n'
    elif not text.endswith('\n'):
        text = f'{text}\n'
    try:
        encoded = text.encode()
    except UnicodeEncodeError as error:
        raise AdapterOutputError(
            OutputFailureKind.MALFORMED,
            'plain response is not valid UTF-8 text',
        ) from error
    if len(encoded) > FINAL_RESPONSE_LIMIT_BYTES:
        raise AdapterOutputError(
            OutputFailureKind.OVERSIZED,
            'plain response exceeds final response limit',
        )
    return encoded


def encode_response(value: JsonObject) -> bytes:
    """Encode one response and enforce the final byte budget.

    Args:
        value: Complete native response object.

    Returns:
        Strict compact JSON bytes ending in one line feed.

    Raises:
        AdapterOutputError: With `MALFORMED` if JSON serialization rejects the
            object or its text cannot be encoded as UTF-8; with `OVERSIZED` if
            the encoded response exceeds the final byte budget.
    """
    try:
        encoded = dump_object(value)
    except (JsonProtocolError, UnicodeEncodeError) as error:
        raise AdapterOutputError(
            OutputFailureKind.MALFORMED,
            'structured response is not valid UTF-8 text',
        ) from error
    if len(encoded) > FINAL_RESPONSE_LIMIT_BYTES:
        raise AdapterOutputError(
            OutputFailureKind.OVERSIZED,
            'structured response exceeds final response limit',
        )
    return encoded


def labeled(step: Step, text: str) -> str:
    """Return one stable step-attributed contribution.

    Args:
        step: Child that supplied the text.
        text: Native or wrapper-generated contribution.

    Returns:
        Stable label followed by the original text.
    """
    return f'[step {step.index}: {step.name}]\n{text}'


def optional_string(value: Mapping[str, object], field_name: str) -> str | None:
    """Validate and return one optional native string field.

    Args:
        value: Native object to read.
        field_name: Field that must be a string when present.

    Returns:
        The string, or `None` when the field is absent.

    Raises:
        AdapterOutputError: If the field is present and not a string.
    """
    item = value.get(field_name)
    if field_name in value and not isinstance(item, str):
        raise AdapterOutputError(OutputFailureKind.UNSUPPORTED, f'{field_name} must be a string')
    return item if isinstance(item, str) else None


def reject_unsupported(message: str) -> NoReturn:
    """Raise one unsupported-output classification.

    Args:
        message: Rejection detail.

    Raises:
        AdapterOutputError: Always, with the `UNSUPPORTED` category.
    """
    raise AdapterOutputError(OutputFailureKind.UNSUPPORTED, message)


def unsupported_fields(value: Mapping[str, object], allowed: Collection[str], label: str) -> None:
    """Reject the first unknown field of a native object.

    Args:
        value: Native object to check.
        allowed: Field names the host accepts.
        label: Prefix for the rejection detail.

    Raises:
        AdapterOutputError: If `value` has a field outside `allowed`.
    """
    unknown = set(value) - set(allowed)
    if unknown:
        reject_unsupported(f'{label}: {min(unknown)}')
