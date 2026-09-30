"""Validate and compose Claude Code hook events."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import TYPE_CHECKING, cast

from sequential_hooks.hosts import (
    API_VERSION,
    AdapterOutputError,
    Composer,
    Contribution,
    HostInputError,
    NativeResponse,
    Outcome,
    OutputFailureKind,
    encode_response,
    labeled,
    optional_string,
    reject_unsupported,
    unsupported_fields,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from plugins.claude._sequential_hooks._doctor import ClaudeDoctorLoader
    from sequential_hooks.hosts import (
        Diagnostics,
        DoctorLoader,
        FinalResponse,
        JsonObject,
        JsonValue,
        Step,
    )

_CLAUDE_FIELD_LIMIT_CHARACTERS = 10_000
_CLAUDE_PRE_RANK = {
    'allow': 1,
    'ask': 2,
    'defer': 3,
    'deny': 4,
}
_COMMON_FIELDS = frozenset(
    {
        'continue',
        'decision',
        'hookSpecificOutput',
        'reason',
        'stopReason',
        'suppressOutput',
        'systemMessage',
        'terminalSequence',
    }
)
_EVENTS = ('PreToolUse', 'PostToolUse')
_POST_HOOK_FIELDS = frozenset(
    {'additionalContext', 'hookEventName', 'updatedMCPToolOutput', 'updatedToolOutput'}
)
_PRE_HOOK_FIELDS = frozenset(
    {
        'additionalContext',
        'hookEventName',
        'permissionDecision',
        'permissionDecisionReason',
        'updatedInput',
    }
)


class ClaudeAdapter(Composer):
    """Compose child results for one validated Claude Code event.

    Child results are consumed in step order. Accepted mutations update the
    event-owned field supplied to later children, and finalization emits the
    composed native response.
    """

    field_limit = _CLAUDE_FIELD_LIMIT_CHARACTERS

    def __init__(
        self,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> None:
        """Validate native input and initialize one Claude Code event adapter.

        Args:
            raw_input: Original strict JSON bytes received from Claude Code.
            payload: Decoded strict JSON object matching `raw_input`.
            diagnostics: Developer-tier diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Raises:
            HostInputError: If required Claude Code input is missing or
                invalid, or the payload nests too deeply to compose.
        """
        event = payload.get('hook_event_name')
        if event not in _EVENTS:
            raise HostInputError('Claude Code input has an unsupported hook event')
        tool_name = payload.get('tool_name')
        if not isinstance(tool_name, str) or not tool_name:
            raise HostInputError('Claude Code input requires a non-empty tool_name')
        if 'tool_input' not in payload:
            raise HostInputError('Claude Code input requires tool_input')
        pre_tool = event == 'PreToolUse'
        if not pre_tool and 'tool_response' not in payload:
            raise HostInputError('Claude Code PostToolUse input requires tool_response')
        self.pre_tool = pre_tool
        self.mutation_field = 'tool_input' if pre_tool else 'tool_response'
        super().__init__(
            event,
            raw_input,
            payload,
            diagnostics,
            allow_on_failure=allow_on_failure,
        )
        self._tool_name = tool_name

    def _parse_post_hook(self, hook: JsonObject) -> Contribution:
        """Validate one current structured PostToolUse result."""
        unsupported_fields(hook, _POST_HOOK_FIELDS, 'unsupported PostToolUse field')
        if hook.get('hookEventName') != 'PostToolUse':
            reject_unsupported('hookSpecificOutput event mismatch')
        has_updated = 'updatedToolOutput' in hook
        has_mcp = 'updatedMCPToolOutput' in hook
        if has_updated and has_mcp:
            reject_unsupported('PostToolUse mutation forms are ambiguous')
        if has_mcp and not self._tool_name.startswith('mcp__'):
            reject_unsupported('updatedMCPToolOutput requires an MCP tool')
        mutation: JsonValue = None
        if has_updated:
            mutation = deepcopy(hook['updatedToolOutput'])
        elif has_mcp:
            mutation = deepcopy(hook['updatedMCPToolOutput'])
        mutation_present = has_updated or has_mcp
        if mutation_present and not _mutation_strings_fit(mutation):
            raise AdapterOutputError(
                OutputFailureKind.UNDELIVERABLE_MUTATION,
                'PostToolUse mutation contains an over-limit string',
            )
        return Contribution(
            additional_context=optional_string(hook, 'additionalContext'),
            mutation=mutation,
            mutation_present=mutation_present,
        )

    def _parse_specific(self, value: JsonObject) -> Contribution:
        """Parse event-specific and deprecated decision fields."""
        legacy_decision = value.get('decision')
        if 'decision' in value and not isinstance(legacy_decision, str):
            reject_unsupported('decision must be a string')
        legacy_reason = optional_string(value, 'reason')
        hook_value = value.get('hookSpecificOutput')
        if 'hookSpecificOutput' in value and not isinstance(hook_value, dict):
            reject_unsupported('hookSpecificOutput must be an object')
        hook = cast('JsonObject', hook_value) if hook_value is not None else None
        if self.pre_tool:
            legacy = _parse_legacy_pre(legacy_decision, legacy_reason)
            if hook is not None and legacy_decision is not None:
                reject_unsupported('current and deprecated decision forms are ambiguous')
            if hook is None:
                return legacy
            return _parse_current_pre(hook)
        if legacy_decision is not None and legacy_decision != 'block':
            reject_unsupported('PostToolUse decision must be block')
        if legacy_reason is not None and legacy_decision != 'block':
            reject_unsupported('PostToolUse reason requires decision block')
        hook_contribution = Contribution() if hook is None else self._parse_post_hook(hook)
        return Contribution(
            additional_context=hook_contribution.additional_context,
            decision=cast('str | None', legacy_decision),
            decision_reason=legacy_reason,
            mutation=hook_contribution.mutation,
            mutation_present=hook_contribution.mutation_present,
        )

    def _post_response(self, response: JsonObject) -> None:
        """Add PostToolUse decision, context, and mutation fields."""
        state = self.state
        if state.decision == 'block':
            response['decision'] = 'block'
            reasons = self.bounded_decision_reasons()
            if reasons is not None:
                response['reason'] = reasons
        contexts = self.bounded_contexts()
        if contexts is None and not state.updated_output_present:
            return
        hook: JsonObject = {'hookEventName': 'PostToolUse'}
        if contexts is not None:
            hook['additionalContext'] = contexts
        if state.updated_output_present:
            hook['updatedToolOutput'] = state.updated_output
        response['hookSpecificOutput'] = hook

    def _pre_response(self, response: JsonObject) -> None:
        """Add PreToolUse output for the winning decision."""
        state = self.state
        if state.decision is None:
            return
        hook: JsonObject = {'hookEventName': 'PreToolUse', 'permissionDecision': state.decision}
        reasons = self.bounded_decision_reasons()
        if reasons is not None:
            hook['permissionDecisionReason'] = reasons
        if state.decision in ('allow', 'ask'):
            contexts = self.bounded_contexts()
            if contexts is not None:
                hook['additionalContext'] = contexts
            if state.updated_input_present:
                hook['updatedInput'] = state.updated_input
        response['hookSpecificOutput'] = hook

    def apply_decision(self, step: Step, value: Contribution) -> None:
        """Compose one native decision and its rank-owned reason.

        PostToolUse accepts only `block`, which always wins. PreToolUse ranks
        `allow`, `ask`, `defer`, `deny`; a higher rank replaces the reasons, an
        equal rank appends its reason.

        Args:
            step: Child that supplied `value`.
            value: Validated contribution whose `decision` may be set.
        """
        state = self.state
        if value.decision is None:
            return
        if not self.pre_tool:
            state.decision = value.decision
            if value.decision_reason is not None:
                state.decision_reasons.append(labeled(step, value.decision_reason))
            return
        current_rank = _CLAUDE_PRE_RANK[state.decision] if state.decision is not None else 0
        new_rank = _CLAUDE_PRE_RANK[value.decision]
        if new_rank > current_rank:
            state.decision = value.decision
            state.decision_reasons = []
        if new_rank >= current_rank and state.decision == value.decision and value.decision_reason:
            state.decision_reasons.append(labeled(step, value.decision_reason))

    def build_response(self) -> JsonObject:
        """Build the current structured Claude Code response.

        Returns:
            Universal fields, the event-specific `hookSpecificOutput`, and the
            global stop, after the current shedding choices.
        """
        state = self.state
        response: JsonObject = {}
        messages = self.bounded_messages()
        if messages is not None:
            response['systemMessage'] = messages
        if state.suppress_output is not None:
            response['suppressOutput'] = state.suppress_output
        if state.terminal_sequence is not None:
            response['terminalSequence'] = state.terminal_sequence
        if self.pre_tool:
            self._pre_response(response)
        else:
            self._post_response(response)
        if state.continue_false:
            response['continue'] = False
            stop_reason = self.bounded_stop_reason()
            if stop_reason is not None:
                response['stopReason'] = stop_reason
        return response

    def classify_unparsable(self, text: str, stripped: bytes, error: str) -> Outcome:
        """Classify non-JSON stdout as native plain text unless it opens an object.

        Args:
            text: Decoded stdout.
            stripped: Stdout without leading ASCII whitespace.
            error: Parser detail for the malformed classification.

        Returns:
            Plain outcome, or a malformed failure for text starting with `{`.
        """
        if not stripped.startswith(b'{'):
            return Outcome('plain', plain=text)
        return Outcome('failure', failure=OutputFailureKind.MALFORMED.value, message=error)

    def failure_texts(self, category: str, message: str) -> tuple[str, str, str]:
        """Return Claude Code's full-detail diagnostic and report texts.

        Args:
            category: Failure category with underscores replaced by spaces.
            message: Failure detail, possibly empty.

        Returns:
            Diagnostic text, report text, and the minimal report text.
        """
        detail = message or f'{category} output'
        return (
            f'{category}: {detail}',
            f'{category}: {detail}',
            (
                f'{category}: sequential-hooks cannot represent hook output; '
                'see developer diagnostics'
            ),
        )

    def parse_structured(self, value: JsonObject) -> Contribution:
        """Parse one whole Claude Code protocol object.

        Args:
            value: Strict JSON object a child wrote to stdout.

        Returns:
            Validated contribution.

        Raises:
            AdapterOutputError: If the object is outside the documented
                contract.
        """
        unsupported_fields(value, _COMMON_FIELDS, 'unsupported Claude Code output field')
        universal = _parse_universal(value)
        specific = self._parse_specific(value)
        return Contribution(
            additional_context=specific.additional_context,
            continue_value=universal.continue_value,
            decision=specific.decision,
            decision_reason=specific.decision_reason,
            mutation=specific.mutation,
            mutation_present=specific.mutation_present,
            stop_reason=universal.stop_reason,
            suppress_output=universal.suppress_output,
            system_message=universal.system_message,
            terminal_sequence=universal.terminal_sequence,
        )

    def terminal_fallback(self, step: Step) -> bool:
        """Drop an accepted mutation to deliver a native block.

        A committed terminal block outranks a retained mutation; the tool
        already ran or will not run, so dropping the rewrite does not run an
        unrewritten operation.

        Args:
            step: Child whose blocking exit is being delivered.

        Returns:
            Whether the response fits without the mutation.
        """
        state = self.state
        state.updated_input_present = False
        state.updated_output_present = False
        self.write_diagnostic(step, 'accepted mutation dropped to deliver the blocking response')
        try:
            self.fit_response()
        except AdapterOutputError:
            return False
        return True

    def undeliverable_owner(self, owner: Contribution) -> bool:
        """Return whether an unfittable owner carries mandatory Claude Code output.

        Args:
            owner: Contribution whose publication cannot fit.

        Returns:
            `True` for a decision, a global stop, or a mutation, unless the
            owner's terminal sequence alone is what does not fit.
        """
        mandatory = (
            owner.continue_value is False or owner.decision is not None or owner.mutation_present
        )
        if (
            mandatory
            and self.state.terminal_sequence_owner is owner
            and self.fits_without_terminal_sequence()
        ):
            return False
        return mandatory


class ClaudeHost:
    """Describe the Claude Code host for entry-point discovery.

    Attributes:
        api_version: Host contract version implemented here.
        events: Native events the wrapper serves.
        name: Entry-point name selected by `--agent`.
        requires_event: `False`; the payload carries `hook_event_name`.
    """

    api_version = API_VERSION
    events = _EVENTS
    name = 'claude'
    requires_event = False

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> ClaudeAdapter:
        """Validate native input and construct the adapter for one invocation.

        Args:
            event: Trusted event established from the payload.
            raw_input: Original native strict JSON hook input.
            payload: Parsed native strict JSON object.
            diagnostics: Bounded developer diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Returns:
            Adapter ready to drive the chain.

        Raises:
            HostInputError: If the payload is not valid input for `event`.
        """
        adapter = ClaudeAdapter(raw_input, payload, diagnostics, allow_on_failure=allow_on_failure)
        if adapter.event != event:
            raise HostInputError('Claude Code input event does not match the selected event')
        return adapter

    def configuration_response(
        self,
        event: str,
        reason: str,
        diagnostics: Diagnostics,
    ) -> FinalResponse:
        """Return the native deny or block for a configuration error.

        Args:
            event: Trusted event, one of `events`.
            reason: Wrapper diagnostic, published after response resolution.
            diagnostics: Bounded developer diagnostic sink.

        Returns:
            PreToolUse deny or PostToolUse block without adapter construction.

        Raises:
            ValueError: If `event` is not one of `events`.
        """
        del reason
        if event not in self.events:
            raise ValueError(f'Claude Code has no configuration response for {event!r}')
        message = '[step 1: configuration]\nspawn: sequential-hooks could not safely run this hook'
        diagnostics.write_global(
            'configuration: spawn: sequential-hooks could not safely run this hook'
        )
        value: JsonObject = {'systemMessage': message}
        if event == 'PreToolUse':
            value['hookSpecificOutput'] = {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': message,
            }
        else:
            value['decision'] = 'block'
            value['reason'] = message
        return NativeResponse(encode_response(value))

    def doctor_loader(self) -> DoctorLoader:
        """Return the static Claude Code configuration loader.

        Returns:
            Loader over the documented Claude Code settings files.
        """
        module = import_module('plugins.claude._sequential_hooks._doctor')
        loader_type: type[ClaudeDoctorLoader] = module.ClaudeDoctorLoader
        return loader_type()

    def payload_event(self, payload: Mapping[str, object]) -> str | None:
        """Return the event named by `hook_event_name`, if supported.

        Args:
            payload: Parsed native strict JSON object.

        Returns:
            `PreToolUse` or `PostToolUse`, else `None`.
        """
        value = payload.get('hook_event_name')
        return value if isinstance(value, str) and value in _EVENTS else None


def _mutation_strings_fit(value: JsonValue) -> bool:
    """Return whether every nested mutation string fits the field cap exactly."""
    if isinstance(value, str):
        return len(value) <= _CLAUDE_FIELD_LIMIT_CHARACTERS
    if isinstance(value, list):
        return all(_mutation_strings_fit(item) for item in value)
    if isinstance(value, dict):
        return all(_mutation_strings_fit(item) for item in value.values())
    return True


def _parse_current_pre(hook: JsonObject) -> Contribution:
    """Validate one current structured PreToolUse result."""
    unsupported_fields(hook, _PRE_HOOK_FIELDS, 'unsupported PreToolUse field')
    if hook.get('hookEventName') != 'PreToolUse':
        reject_unsupported('hookSpecificOutput event mismatch')
    native_decision = hook.get('permissionDecision')
    if not isinstance(native_decision, str) or native_decision not in _CLAUDE_PRE_RANK:
        reject_unsupported('unsupported PreToolUse permissionDecision')
    decision = native_decision
    mutation: JsonValue = None
    mutation_present = 'updatedInput' in hook
    if mutation_present:
        if decision not in ('allow', 'ask'):
            reject_unsupported('updatedInput requires allow or ask')
        mutation = deepcopy(hook['updatedInput'])
        if not _mutation_strings_fit(mutation):
            raise AdapterOutputError(
                OutputFailureKind.UNDELIVERABLE_MUTATION,
                'PreToolUse mutation contains an over-limit string',
            )
    return Contribution(
        additional_context=optional_string(hook, 'additionalContext'),
        decision=decision,
        decision_reason=optional_string(hook, 'permissionDecisionReason'),
        mutation=mutation,
        mutation_present=mutation_present,
    )


def _parse_legacy_pre(legacy_decision: JsonValue, legacy_reason: str | None) -> Contribution:
    """Validate and normalize one deprecated top-level PreToolUse result."""
    decision: str | None = None
    if legacy_decision is not None:
        if legacy_decision not in ('approve', 'block'):
            reject_unsupported('deprecated decision must be approve or block')
        decision = 'allow' if legacy_decision == 'approve' else 'deny'
    elif legacy_reason is not None:
        reject_unsupported('reason requires a deprecated decision')
    return Contribution(decision=decision, decision_reason=legacy_reason)


def _parse_universal(value: JsonObject) -> Contribution:
    """Validate and retain universal Claude Code output fields."""
    continue_value = value.get('continue')
    if 'continue' in value and not isinstance(continue_value, bool):
        reject_unsupported('continue must be a Boolean')
    stop_reason = optional_string(value, 'stopReason')
    if stop_reason is not None and continue_value is not False:
        reject_unsupported('stopReason requires continue false')
    suppress_output = value.get('suppressOutput')
    if 'suppressOutput' in value and not isinstance(suppress_output, bool):
        reject_unsupported('suppressOutput must be a Boolean')
    terminal_sequence = optional_string(value, 'terminalSequence')
    if terminal_sequence is not None and len(terminal_sequence) > _CLAUDE_FIELD_LIMIT_CHARACTERS:
        raise AdapterOutputError(
            OutputFailureKind.OVERSIZED,
            'terminalSequence exceeds the Claude Code field limit',
        )
    return Contribution(
        continue_value=cast('bool | None', continue_value),
        stop_reason=stop_reason,
        suppress_output=cast('bool | None', suppress_output),
        system_message=optional_string(value, 'systemMessage'),
        terminal_sequence=terminal_sequence,
    )
