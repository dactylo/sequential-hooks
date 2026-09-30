"""Validate and compose Antigravity hook events."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, cast

from sequential_hooks.hosts import (
    API_VERSION,
    TERMINATION_FAILURES,
    UNCONTAINED_WARNING,
    AdapterOutputError,
    HostInputError,
    JsonProtocolError,
    NativeResponse,
    OutputFailureKind,
    continue_after_failure,
    encode_response,
    labeled,
    load_object,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from plugins.agy._sequential_hooks._doctor import AgyDoctorLoader
    from sequential_hooks.hosts import (
        ChainControl,
        Diagnostics,
        DoctorLoader,
        FinalResponse,
        JsonObject,
        JsonValue,
        Step,
        StepResult,
    )

_AGY_PRE_RANK = {
    'neutral': 0,
    'allow': 1,
    'ask': 2,
    'force_ask': 3,
    'deny': 4,
}
_EVENTS = ('PreToolUse', 'PostToolUse')
_MINIMUM_CONFLICTING_OVERRIDE_COUNT = 2


@dataclass(frozen=True)
class _Contribution:
    """Carry one fully validated Antigravity contribution."""

    _decision: str
    _permission_overrides: list[str] | None
    _reason: str | None


@dataclass(frozen=True)
class _RankedContribution:
    """Associate one accepted contribution with its child step."""

    _contribution: _Contribution
    _step: Step


class AgyAdapter:
    """Compose child results for one validated Antigravity event.

    Child results are consumed in step order and composed into an event-
    specific final response. The original strict JSON input remains the input
    for every child.

    Attributes:
        event: Validated pre-tool or post-tool event.
        should_run_steps: Whether the invocation should execute child steps.
    """

    def __init__(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> None:
        """Validate native input and initialize one Antigravity event adapter.

        Args:
            event: Exact CLI event selected by the caller.
            raw_input: Original strict JSON bytes received from Antigravity.
            payload: Decoded strict JSON object matching `raw_input`.
            diagnostics: Developer-tier diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Raises:
            HostInputError: If required Antigravity input is missing, has the
                wrong type, or is invalid.
        """
        if event not in _EVENTS:
            raise HostInputError('Antigravity input has an unsupported event')
        step_index = payload.get('stepIdx')
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            raise HostInputError('Antigravity input requires an integer stepIdx')
        if event == 'PreToolUse':
            tool_call = payload.get('toolCall')
            if not isinstance(tool_call, dict):
                raise HostInputError('Antigravity PreToolUse input requires object toolCall')
            tool_name = tool_call.get('name')
            if not isinstance(tool_name, str):
                raise HostInputError('Antigravity PreToolUse input requires string toolCall.name')
            if not tool_name:
                raise HostInputError(
                    'Antigravity PreToolUse input requires non-empty toolCall.name'
                )
            if not isinstance(tool_call.get('args'), dict):
                raise HostInputError('Antigravity PreToolUse input requires object toolCall.args')
        self._should_run_steps = not (
            event == 'PostToolUse' and 'toolCall' in payload and payload['toolCall'] is None
        )
        self._allow_on_failure = allow_on_failure
        self._diagnostics = diagnostics
        self._event = event
        self._raw_input = raw_input
        self._response_bytes: bytes | None = None
        self._uncontained_warning_stored = False
        self._warnings: list[str] = []
        self._winning: list[_RankedContribution] = []
        self._winning_rank = 0

    def _classify_pre(self, value: JsonObject) -> _Contribution:
        """Validate one complete Antigravity pre-tool object."""
        allowed_fields = {'decision', 'permissionOverrides', 'reason'}
        unknown_fields = set(value) - allowed_fields
        if unknown_fields:
            field_count = len(unknown_fields)
            field_label = 'field' if field_count == 1 else 'fields'
            raise AdapterOutputError(
                OutputFailureKind.UNSUPPORTED,
                f'Antigravity output has {field_count} unsupported {field_label}',
            )
        decision = value.get('decision')
        if not isinstance(decision, str) or decision not in _AGY_PRE_RANK:
            raise AdapterOutputError(
                OutputFailureKind.MALFORMED,
                'Antigravity pre-tool output requires a supported decision',
            )
        reason_value = value.get('reason')
        if 'reason' in value and not isinstance(reason_value, str):
            raise AdapterOutputError(
                OutputFailureKind.MALFORMED,
                'Antigravity reason must be a string',
            )
        reason = reason_value if isinstance(reason_value, str) else None
        overrides_value = value.get('permissionOverrides')
        overrides: list[str] | None = None
        if 'permissionOverrides' in value:
            if not isinstance(overrides_value, list) or not all(
                isinstance(item, str) for item in overrides_value
            ):
                raise AdapterOutputError(
                    OutputFailureKind.MALFORMED,
                    'Antigravity permissionOverrides must be an array of strings',
                )
            overrides = cast('list[str]', overrides_value)
        return _Contribution(decision, overrides, reason)

    def _conflicting_override_steps(
        self,
        contributions: list[_RankedContribution],
    ) -> list[Step]:
        """Return winning steps when their non-empty override arrays conflict."""
        with_overrides = [
            item for item in contributions if item._contribution._permission_overrides
        ]
        if len(with_overrides) < _MINIMUM_CONFLICTING_OVERRIDE_COUNT:
            return []
        first = with_overrides[0]._contribution._permission_overrides
        if all(item._contribution._permission_overrides == first for item in with_overrides):
            return []
        return [item._step for item in with_overrides]

    def _winning_overrides(
        self,
        contributions: list[_RankedContribution],
    ) -> list[str] | None:
        """Return the first non-empty override array at the winning rank."""
        return next(
            (
                item._contribution._permission_overrides
                for item in contributions
                if item._contribution._permission_overrides
            ),
            None,
        )

    def _pre_object(
        self,
        contributions: list[_RankedContribution],
        warnings: list[str],
        *,
        shed_reasons: bool = False,
    ) -> JsonObject:
        """Build one native pre-tool response from immutable candidate data."""
        winning_decision = contributions[0]._contribution._decision if contributions else 'allow'
        decision = 'allow' if winning_decision == 'neutral' else winning_decision
        response: JsonObject = {'decision': decision}
        overrides = None if decision == 'deny' else self._winning_overrides(contributions)
        reasons = [
            labeled(item._step, item._contribution._reason)
            for item in contributions
            if item._contribution._reason
        ]
        if reasons and decision in ('ask', 'force_ask', 'deny'):
            reasons.extend(warnings)
        if reasons and not shed_reasons:
            response['reason'] = '\n\n'.join(reasons)
        if overrides is not None:
            response['permissionOverrides'] = cast('list[JsonValue]', overrides)
        return response

    def _encode_pre_candidate(
        self,
        contributions: list[_RankedContribution],
        warnings: list[str],
        *,
        shed_reasons: bool = False,
    ) -> bytes:
        """Build and encode one native pre-tool response candidate."""
        return encode_response(
            self._pre_object(
                contributions,
                warnings,
                shed_reasons=shed_reasons,
            )
        )

    def _fit_pre_candidate(
        self,
        contributions: list[_RankedContribution],
        warnings: list[str],
    ) -> bytes:
        """Build and encode the bounded native pre-tool response candidate."""
        try:
            return self._encode_pre_candidate(contributions, warnings)
        except AdapterOutputError as error:
            if error.kind is not OutputFailureKind.OVERSIZED:
                raise
        return self._encode_pre_candidate(
            contributions,
            warnings,
            shed_reasons=True,
        )

    def _apply_failure(
        self,
        step: Step,
        failure: str,
        message: str,
        *,
        uncontained: bool = False,
    ) -> ChainControl:
        """Apply the common policy to one classified child failure."""
        candidate_warnings = [*self._warnings]
        warning_stored = self._uncontained_warning_stored
        if uncontained and failure in TERMINATION_FAILURES:
            self._diagnostics.write_step(step, UNCONTAINED_WARNING)
            if not warning_stored:
                candidate_warnings.append(labeled(step, UNCONTAINED_WARNING))
                warning_stored = True
        category = failure.replace('_', ' ')
        detail = message or f'{category} child result'
        self._diagnostics.write_step(step, f'{category}: {detail}')
        warning = labeled(step, f'{category}: {detail}')
        should_continue = continue_after_failure(
            self._event == 'PreToolUse',
            failure,
            allow_on_failure=self._allow_on_failure,
        )
        if should_continue:
            candidate_warnings.append(warning)
            candidate_winning = self._winning
            candidate_rank = self._winning_rank
        else:
            candidate_winning = [
                _RankedContribution(
                    _Contribution('deny', None, f'{category}: {detail}'),
                    step,
                )
            ]
            candidate_rank = _AGY_PRE_RANK['deny']
        try:
            if self._event == 'PostToolUse':
                response_bytes = encode_response({})
            else:
                response_bytes = self._fit_pre_candidate(
                    candidate_winning,
                    candidate_warnings,
                )
        except AdapterOutputError as error:
            if failure in OutputFailureKind:
                raise
            return self._apply_failure(step, error.kind.value, str(error))
        self._uncontained_warning_stored = warning_stored
        self._warnings = candidate_warnings
        self._winning = candidate_winning
        self._winning_rank = candidate_rank
        if should_continue:
            return 'continue'
        self._response_bytes = response_bytes
        return 'stop'

    def _apply_pre(self, step: Step, contribution: _Contribution) -> ChainControl:
        """Apply one fully validated pre-tool contribution."""
        new_rank = _AGY_PRE_RANK[contribution._decision]
        current_rank = self._winning_rank
        terminal = contribution._decision == 'deny'
        if new_rank >= current_rank:
            prior = [] if new_rank > current_rank else self._winning
            candidate = [*prior, _RankedContribution(contribution, step)]
            conflict_steps = self._conflicting_override_steps(candidate)
            if conflict_steps:
                indexes = ', '.join(f'step {item.index}' for item in conflict_steps)
                reason = f'conflicting permissionOverrides from contributing steps: {indexes}'
                new_rank = _AGY_PRE_RANK['deny']
                candidate = [
                    _RankedContribution(
                        _Contribution('deny', None, reason),
                        step,
                    )
                ]
                terminal = True
            try:
                response_bytes = self._encode_pre_candidate(
                    candidate,
                    self._warnings,
                )
            except AdapterOutputError as error:
                if error.kind is not OutputFailureKind.OVERSIZED:
                    return self._apply_failure(step, error.kind.value, str(error))
                try:
                    response_bytes = self._encode_pre_candidate(
                        candidate,
                        self._warnings,
                        shed_reasons=True,
                    )
                except AdapterOutputError:
                    failure = (
                        OutputFailureKind.UNDELIVERABLE_MUTATION
                        if self._winning_overrides(candidate) is not None
                        else OutputFailureKind.OVERSIZED
                    )
                    return self._apply_failure(
                        step,
                        failure.value,
                        'accepted Antigravity contribution cannot fit in the final response',
                    )
            self._winning_rank = new_rank
            self._winning = candidate
            if terminal:
                self._response_bytes = response_bytes
                return 'stop'
        return 'continue'

    def consume(self, step: Step, result: StepResult) -> ChainControl:
        """Apply one child result and choose whether the runner continues.

        Args:
            step: Child that produced `result`.
            result: Bounded process outcome to interpret.

        Returns:
            `'continue'` or `'stop'`.
        """
        if result.failure is not None:
            control = self._apply_failure(
                step,
                str(result.failure.kind),
                result.failure.message,
                uncontained=str(result.containment) == 'uncontained',
            )
        elif result.returncode not in (None, 0):
            control = self._apply_failure(
                step,
                OutputFailureKind.MALFORMED.value,
                f'child exited with status {result.returncode}',
            )
        elif not result.stdout.lstrip(b' \t\r\n'):
            control = 'continue'
        else:
            try:
                value = load_object(result.stdout)
            except JsonProtocolError as error:
                control = self._apply_failure(
                    step,
                    OutputFailureKind.MALFORMED.value,
                    str(error),
                )
            else:
                if self._event == 'PostToolUse':
                    control = (
                        self._apply_failure(
                            step,
                            OutputFailureKind.UNSUPPORTED.value,
                            'Antigravity post-tool output must be an empty object',
                        )
                        if value
                        else 'continue'
                    )
                else:
                    try:
                        contribution = self._classify_pre(value)
                    except AdapterOutputError as error:
                        control = self._apply_failure(step, error.kind.value, str(error))
                    else:
                        control = self._apply_pre(step, contribution)
        return control

    @property
    def event(self) -> str:
        """Return the validated native event.

        Returns:
            Current pre-tool or post-tool event.
        """
        return self._event

    def finalize(self) -> NativeResponse:
        """Return the final native Antigravity response.

        Returns:
            The terminal response retained by a stop, otherwise exactly one
            host response with one trailing line feed.
        """
        if self._response_bytes is not None:
            return NativeResponse(self._response_bytes)
        if self._event == 'PostToolUse':
            response_bytes = encode_response({})
        else:
            response_bytes = self._fit_pre_candidate(
                self._winning,
                self._warnings,
            )
        return NativeResponse(response_bytes)

    @property
    def should_run_steps(self) -> bool:
        """Return whether this invocation should execute child steps.

        Returns:
            `False` only for an explicit-null Antigravity post-tool callback.
        """
        return self._should_run_steps

    def step_input(self) -> bytes:
        """Return the unchanged native JSON input for the next child.

        Returns:
            Original strict JSON input bytes.
        """
        return self._raw_input


class AgyHost:
    """Describe the Antigravity host for entry-point discovery.

    Attributes:
        api_version: Host contract version implemented here.
        events: Native events the wrapper serves.
        name: Entry-point name selected by `--agent`.
        requires_event: `True`; Antigravity payloads carry no event name.
    """

    api_version = API_VERSION
    events = _EVENTS
    name = 'agy'
    requires_event = True

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> AgyAdapter:
        """Validate native input and construct the adapter for one invocation.

        Args:
            event: Trusted event from `--event`.
            raw_input: Original native strict JSON hook input.
            payload: Parsed native strict JSON object.
            diagnostics: Bounded developer diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Returns:
            Adapter ready to drive the chain.

        Raises:
            HostInputError: If the payload is not valid input for `event`.
        """
        return AgyAdapter(
            event,
            raw_input,
            payload,
            diagnostics,
            allow_on_failure=allow_on_failure,
        )

    def configuration_response(
        self,
        event: str,
        reason: str,
        diagnostics: Diagnostics,
    ) -> FinalResponse:
        """Return the top-level deny or the empty post-tool object.

        Args:
            event: Trusted event, one of `events`.
            reason: Wrapper diagnostic, published after response resolution.
            diagnostics: Bounded developer diagnostic sink.

        Returns:
            PreToolUse deny JSON, or `{}` for PostToolUse, which does not
            block.

        Raises:
            ValueError: If `event` is not one of `events`.
        """
        del reason
        if event not in self.events:
            raise ValueError(f'Antigravity has no configuration response for {event!r}')
        message = '[step 1: configuration]\nspawn: sequential-hooks could not safely run this hook'
        diagnostics.write_global(
            'configuration: spawn: sequential-hooks could not safely run this hook'
        )
        value: JsonObject = (
            {'decision': 'deny', 'reason': message} if event == 'PreToolUse' else {}
        )
        return NativeResponse(encode_response(value))

    def doctor_loader(self) -> DoctorLoader:
        """Return the static Antigravity configuration loader.

        Returns:
            Loader over the documented Antigravity hooks files.
        """
        module = import_module('plugins.agy._sequential_hooks._doctor')
        loader_type: type[AgyDoctorLoader] = module.AgyDoctorLoader
        return loader_type()

    def payload_event(self, payload: Mapping[str, object]) -> str | None:
        """Return `None`; Antigravity payloads never carry the event.

        Args:
            payload: Parsed native strict JSON object.

        Returns:
            Always `None`.
        """
        del payload
        return None
