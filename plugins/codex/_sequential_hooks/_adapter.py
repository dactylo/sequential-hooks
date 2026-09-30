"""Validate and compose Codex hook events."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import TYPE_CHECKING, cast

from sequential_hooks.hosts import (
    API_VERSION,
    Composer,
    Contribution,
    HostInputError,
    NativeResponse,
    Outcome,
    OutputFailureKind,
    bounded_diagnostic,
    encode_response,
    labeled,
    optional_string,
    reject_unsupported,
    unsupported_fields,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from plugins.codex._sequential_hooks._doctor import CodexDoctorLoader
    from sequential_hooks.hosts import (
        Diagnostics,
        DoctorLoader,
        FinalResponse,
        JsonObject,
        JsonValue,
        Step,
    )

_COMMAND_TOOLS = ('Bash', 'apply_patch')
_EVENTS = ('PreToolUse', 'PostToolUse')
_POST_FIELDS = frozenset(
    {'continue', 'decision', 'hookSpecificOutput', 'reason', 'stopReason', 'systemMessage'}
)
_POST_HOOK_FIELDS = frozenset({'additionalContext', 'hookEventName'})
_PRE_FIELDS = frozenset({'decision', 'hookSpecificOutput', 'reason', 'systemMessage'})
_PRE_HOOK_FIELDS = frozenset(
    {
        'additionalContext',
        'hookEventName',
        'permissionDecision',
        'permissionDecisionReason',
        'updatedInput',
    }
)


class CodexAdapter(Composer):
    """Compose child results for one validated Codex event.

    Child results are consumed in step order. Accepted mutations replace the
    `tool_input` supplied to later children, and finalization emits the
    composed native response.
    """

    def __init__(
        self,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> None:
        """Validate native input and initialize one Codex event adapter.

        Args:
            raw_input: Original strict JSON bytes received from Codex.
            payload: Decoded strict JSON object matching `raw_input`.
            diagnostics: Developer-tier diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Raises:
            HostInputError: If required Codex input is missing or invalid, or
                the payload nests too deeply to compose.
        """
        event = payload.get('hook_event_name')
        if event not in _EVENTS:
            raise HostInputError('Codex input has an unsupported hook event')
        tool_name = payload.get('tool_name')
        if not isinstance(tool_name, str) or not tool_name:
            raise HostInputError('Codex input requires a non-empty tool_name')
        if 'tool_input' not in payload:
            raise HostInputError('Codex input requires tool_input')
        pre_tool = event == 'PreToolUse'
        if not pre_tool and 'tool_response' not in payload:
            raise HostInputError('Codex PostToolUse input requires tool_response')
        self.pre_tool = pre_tool
        self.mutation_field = 'tool_input' if pre_tool else None
        super().__init__(
            event,
            raw_input,
            payload,
            diagnostics,
            allow_on_failure=allow_on_failure,
        )
        self._tool_name = tool_name

    def _parse_pre_mutation(self, hook: JsonObject, decision: object) -> JsonValue:
        """Validate one whole Codex replacement arguments object."""
        mutation_present = 'updatedInput' in hook
        if decision == 'allow' and not mutation_present:
            reject_unsupported('permissionDecision allow requires updatedInput')
        if mutation_present and decision != 'allow':
            reject_unsupported('updatedInput requires permissionDecision allow')
        if not mutation_present:
            return None
        mutation_value = hook['updatedInput']
        if not isinstance(mutation_value, dict):
            reject_unsupported('updatedInput must be an arguments object')
        mutation = deepcopy(cast('JsonObject', mutation_value))
        if self._tool_name in _COMMAND_TOOLS and not isinstance(mutation.get('command'), str):
            reject_unsupported(f'{self._tool_name} updatedInput requires string command')
        return mutation

    def _parse_pre_hook(self, hook: JsonObject) -> Contribution:
        """Validate current Codex PreToolUse hook fields."""
        unsupported_fields(hook, _PRE_HOOK_FIELDS, 'unsupported Codex PreToolUse hook field')
        if hook.get('hookEventName') != 'PreToolUse':
            reject_unsupported('hookSpecificOutput event mismatch')
        decision = hook.get('permissionDecision')
        if 'permissionDecision' in hook and decision not in ('allow', 'deny'):
            reject_unsupported('unsupported Codex PreToolUse permissionDecision')
        decision_reason = optional_string(hook, 'permissionDecisionReason')
        if decision_reason is not None and decision is None:
            reject_unsupported('permissionDecisionReason requires a decision')
        return Contribution(
            additional_context=optional_string(hook, 'additionalContext'),
            decision=cast('str | None', decision),
            decision_reason=decision_reason,
            mutation=self._parse_pre_mutation(hook, decision),
            mutation_present='updatedInput' in hook,
        )

    def _parse_pre(self, value: JsonObject) -> Contribution:
        """Validate one structured PreToolUse result."""
        unsupported_fields(value, _PRE_FIELDS, 'unsupported Codex PreToolUse field')
        system_message = optional_string(value, 'systemMessage')
        legacy = _parse_pre_legacy(value)
        if 'hookSpecificOutput' not in value:
            return Contribution(
                decision=legacy.decision,
                decision_reason=legacy.decision_reason,
                system_message=system_message,
            )
        hook_value = value['hookSpecificOutput']
        if not isinstance(hook_value, dict):
            reject_unsupported('hookSpecificOutput must be an object')
        current = self._parse_pre_hook(cast('JsonObject', hook_value))
        if legacy.decision is not None and current.decision is not None:
            reject_unsupported('current and deprecated decision forms are ambiguous')
        return Contribution(
            additional_context=current.additional_context,
            decision=current.decision if legacy.decision is None else legacy.decision,
            decision_reason=(
                current.decision_reason if legacy.decision is None else legacy.decision_reason
            ),
            mutation=current.mutation,
            mutation_present=current.mutation_present,
            system_message=system_message,
        )

    def _post_response(self, response: JsonObject) -> None:
        """Add PostToolUse block, context, and global stop fields."""
        state = self.state
        if state.decision == 'block':
            response['decision'] = 'block'
            reasons = self.bounded_decision_reasons()
            if reasons is not None:
                response['reason'] = reasons
        contexts = self.bounded_contexts()
        if contexts is not None:
            response['hookSpecificOutput'] = {
                'hookEventName': 'PostToolUse',
                'additionalContext': contexts,
            }
        if state.continue_false:
            response['continue'] = False
            stop_reason = self.bounded_stop_reason()
            if stop_reason is not None:
                response['stopReason'] = stop_reason

    def _pre_response(self, response: JsonObject) -> None:
        """Add PreToolUse output for the winning result."""
        state = self.state
        contexts = self.bounded_contexts()
        if state.decision is None and contexts is None:
            return
        hook: JsonObject = {'hookEventName': 'PreToolUse'}
        if state.decision is not None:
            hook['permissionDecision'] = state.decision
            reasons = self.bounded_decision_reasons()
            if reasons is not None:
                hook['permissionDecisionReason'] = reasons
        if contexts is not None and state.decision != 'deny':
            hook['additionalContext'] = contexts
        if state.decision == 'allow' and state.updated_input_present:
            hook['updatedInput'] = state.updated_input
        response['hookSpecificOutput'] = hook

    def apply_decision(self, step: Step, value: Contribution) -> None:
        """Compose decisions: deny outranks non-decisions, and the wrapper never creates ask.

        Args:
            step: Child that supplied `value`.
            value: Validated contribution whose `decision` may be set.
        """
        state = self.state
        if value.decision is None:
            return
        reason = (
            [labeled(step, value.decision_reason)] if value.decision_reason is not None else []
        )
        if not self.pre_tool:
            state.decision = 'block'
            state.decision_reasons = reason
            return
        if value.decision == 'deny':
            state.decision = 'deny'
            state.decision_reasons = reason
            return
        if state.decision is None:
            state.decision = 'allow'
            state.decision_reasons = []
        if state.decision == 'allow':
            state.decision_reasons.extend(reason)

    def build_response(self) -> JsonObject:
        """Build the current canonical Codex response.

        Returns:
            User messages and the event-specific fields after the current
            shedding choices.
        """
        response: JsonObject = {}
        messages = self.bounded_messages()
        if messages is not None:
            response['systemMessage'] = messages
        if self.pre_tool:
            self._pre_response(response)
        else:
            self._post_response(response)
        return response

    def classify_unparsable(self, text: str, stripped: bytes, error: str) -> Outcome:
        """Classify non-JSON stdout as developer text unless it opens JSON.

        Args:
            text: Decoded stdout.
            stripped: Stdout without leading ASCII whitespace.
            error: Parser detail for the malformed classification.

        Returns:
            Developer outcome, or a malformed failure for text starting with
            `{` or `[`.
        """
        if stripped[:1] in (b'{', b'['):
            return Outcome('failure', failure=OutputFailureKind.MALFORMED.value, message=error)
        return Outcome('developer', developer_text=text)

    def failure_texts(self, category: str, message: str) -> tuple[str, str, str]:
        """Return Codex's repr-quoted diagnostic and pointer report texts.

        Args:
            category: Failure category with underscores replaced by spaces.
            message: Failure detail, possibly empty.

        Returns:
            Bounded diagnostic text, report text, and minimal report text.
        """
        detail = message or f'{category} child result'
        return (
            bounded_diagnostic(f'{category}: ', detail),
            f'{category}: child result rejected; see developer diagnostics',
            f'{category}: see developer diagnostics',
        )

    def ordinary_exit_texts(self, returncode: int) -> tuple[str, str]:
        """Return the diagnostic and pointer report for a non-blocking exit.

        Args:
            returncode: Child status.

        Returns:
            Diagnostic text and report text.
        """
        detail = f'exited with status {returncode}'
        return detail, f'{detail}; see developer diagnostics'

    def parse_structured(self, value: JsonObject) -> Contribution:
        """Parse one complete Codex protocol object.

        Args:
            value: Strict JSON object a child wrote to stdout.

        Returns:
            Validated contribution.

        Raises:
            AdapterOutputError: If the object is outside the documented
                contract.
        """
        return self._parse_pre(value) if self.pre_tool else _parse_post(value)


class CodexHost:
    """Describe the Codex host for entry-point discovery.

    Attributes:
        api_version: Host contract version implemented here.
        events: Native events the wrapper serves.
        name: Entry-point name selected by `--agent`.
        requires_event: `False`; the payload carries `hook_event_name`.
    """

    api_version = API_VERSION
    events = _EVENTS
    name = 'codex'
    requires_event = False

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> CodexAdapter:
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
        adapter = CodexAdapter(raw_input, payload, diagnostics, allow_on_failure=allow_on_failure)
        if adapter.event != event:
            raise HostInputError('Codex input event does not match the selected event')
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
            raise ValueError(f'Codex has no configuration response for {event!r}')
        message = (
            '[step 1: configuration]\nspawn: child result rejected; see developer diagnostics'
        )
        diagnostics.write_global(
            "configuration: spawn: 'sequential-hooks could not safely run this hook'"
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
        """Return the static Codex configuration loader.

        Returns:
            Loader over the documented Codex hooks and config files.
        """
        module = import_module('plugins.codex._sequential_hooks._doctor')
        loader_type: type[CodexDoctorLoader] = module.CodexDoctorLoader
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


def _parse_post(value: JsonObject) -> Contribution:
    """Validate one structured PostToolUse result."""
    unsupported_fields(value, _POST_FIELDS, 'unsupported Codex PostToolUse field')
    decision = value.get('decision')
    if 'decision' in value and decision != 'block':
        reject_unsupported('Codex PostToolUse decision must be block')
    reason = optional_string(value, 'reason')
    if reason is not None and decision != 'block':
        reject_unsupported('Codex PostToolUse reason requires decision block')
    continue_value = value.get('continue')
    if 'continue' in value and not isinstance(continue_value, bool):
        reject_unsupported('continue must be a Boolean')
    stop_reason = optional_string(value, 'stopReason')
    if stop_reason is not None and continue_value is not False:
        reject_unsupported('stopReason requires continue false')
    additional_context: str | None = None
    if 'hookSpecificOutput' in value:
        hook_value = value['hookSpecificOutput']
        if not isinstance(hook_value, dict):
            reject_unsupported('hookSpecificOutput must be an object')
        hook = cast('JsonObject', hook_value)
        unsupported_fields(hook, _POST_HOOK_FIELDS, 'unsupported Codex PostToolUse hook field')
        if hook.get('hookEventName') != 'PostToolUse':
            reject_unsupported('hookSpecificOutput event mismatch')
        additional_context = optional_string(hook, 'additionalContext')
    return Contribution(
        additional_context=additional_context,
        continue_value=cast('bool | None', continue_value),
        decision=cast('str | None', decision),
        decision_reason=reason,
        stop_reason=stop_reason,
        system_message=optional_string(value, 'systemMessage'),
    )


def _parse_pre_legacy(value: JsonObject) -> Contribution:
    """Validate deprecated Codex PreToolUse decision fields."""
    legacy_decision = value.get('decision')
    if 'decision' in value and legacy_decision != 'block':
        reject_unsupported('deprecated Codex decision must be block')
    legacy_reason = optional_string(value, 'reason')
    if legacy_reason is not None and legacy_decision != 'block':
        reject_unsupported('reason requires deprecated decision block')
    return Contribution(
        decision='deny' if legacy_decision == 'block' else None,
        decision_reason=legacy_reason,
    )
