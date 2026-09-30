# Throwaway third-party host used only to prove the public contract.
# It imports nothing private from sequential-hooks and returns its own response
# objects. Its protocol is invented: input carries `tool` and `arguments`,
# children answer with `verdict`, `reason`, `note`, and `arguments`, and the
# final response contains `verdict`, `arguments`, and `notes`.

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from sequential_hooks.hosts import (
    API_VERSION,
    Composer,
    ConfigSource,
    Contribution,
    Finding,
    FindingSeverity,
    HostInputError,
    LaunchSemantics,
    Registration,
    SourceKind,
    labeled,
    optional_string,
    reject_unsupported,
    unsupported_fields,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sequential_hooks.hosts import (
        Diagnostics,
        DoctorLoader,
        InspectedCommand,
        JsonObject,
        Step,
    )

EVENTS = ('BeforeTool', 'AfterTool')
_FIELDS = frozenset({'arguments', 'note', 'reason', 'verdict'})
_VERDICTS = ('allow', 'deny')


class ExampleLoader:
    def analyze_registration(
        self,
        registration: Registration,
        inspected: InspectedCommand,
        *,
        windows: bool,
    ) -> tuple[Finding, ...]:
        del inspected, windows
        if registration.matcher is None:
            return (
                Finding(
                    FindingSeverity.INFO,
                    'example_unmatched',
                    'example registration applies to every tool',
                ),
            )
        return ()

    def discover_sources(
        self, home: Path, cwd: Path, system_root: Path
    ) -> tuple[ConfigSource, ...]:
        del system_root
        candidates = (
            (home / '.example' / 'hooks.json', SourceKind.USER),
            (cwd / '.example' / 'hooks.json', SourceKind.PROJECT),
        )
        return tuple(ConfigSource(path, kind, path.is_file()) for path, kind in candidates)

    def inspection_limitations(self) -> tuple[str, ...]:
        return ('example hooks registered through environment variables are not inspected',)

    def load_registrations(
        self,
        sources: tuple[ConfigSource, ...],
    ) -> tuple[tuple[Registration, ...], tuple[Finding, ...]]:
        registrations: list[Registration] = []
        findings: list[Finding] = []
        for source in sources:
            if not source.exists:
                continue
            try:
                document = cast('object', json.loads(source.path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                findings.append(
                    Finding(
                        FindingSeverity.ERROR,
                        'source_unreadable',
                        f'{source.path} is not valid JSON',
                    )
                )
                continue
            table = cast('dict[str, object]', document) if isinstance(document, dict) else {}
            hooks = table.get('hooks')
            for hook in cast('list[object]', hooks) if isinstance(hooks, list) else []:
                if not isinstance(hook, dict):
                    continue
                entry = cast('dict[str, object]', hook)
                command = entry.get('command')
                event = entry.get('event')
                if not isinstance(command, str) or not isinstance(event, str):
                    continue
                matcher = entry.get('matcher')
                registrations.append(
                    Registration(
                        source=source,
                        event=event,
                        matcher=matcher if isinstance(matcher, str) else None,
                        command=command,
                        args=None,
                        launch_semantics=LaunchSemantics.POSIX_SHELL,
                        command_windows=None,
                        windows_launch_semantics=None,
                        timeout=None,
                        enabled=True,
                        type='command',
                    )
                )
        return tuple(registrations), tuple(findings)


@dataclass(frozen=True)
class ExampleResponse:
    stdout: bytes
    exit_code: int = 0


class ExampleAdapter(Composer):
    def __init__(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> None:
        if not isinstance(payload.get('tool'), str):
            raise HostInputError('example input requires a string tool')
        pre_tool = event == 'BeforeTool'
        if pre_tool and not isinstance(payload.get('arguments'), dict):
            raise HostInputError('example BeforeTool input requires an arguments object')
        self.pre_tool = pre_tool
        self.mutation_field = 'arguments' if pre_tool else None
        super().__init__(
            event,
            raw_input,
            payload,
            diagnostics,
            allow_on_failure=allow_on_failure,
        )

    def apply_decision(self, step: Step, value: Contribution) -> None:
        state = self.state
        if value.decision is None:
            return
        reasons = [labeled(step, value.decision_reason)] if value.decision_reason else []
        if value.decision == 'deny' or state.decision is None:
            state.decision = value.decision
            state.decision_reasons = reasons
        elif state.decision == 'allow':
            state.decision_reasons.extend(reasons)

    def build_response(self) -> JsonObject:
        state = self.state
        response: JsonObject = {}
        if state.decision is not None:
            response['verdict'] = 'deny' if state.decision == 'deny' else 'allow'
            reasons = self.bounded_decision_reasons()
            if reasons is not None:
                response['reason'] = reasons
        if state.decision == 'allow' and state.updated_input_present:
            response['arguments'] = state.updated_input
        notes = self.bounded_messages()
        if notes is not None:
            response['notes'] = notes
        return response

    def finalize(self) -> ExampleResponse:
        response = super().finalize()
        return ExampleResponse(response.stdout, response.exit_code)

    def parse_structured(self, value: JsonObject) -> Contribution:
        unsupported_fields(value, _FIELDS, 'unsupported example field')
        verdict = value.get('verdict')
        if 'verdict' in value and verdict not in _VERDICTS:
            reject_unsupported('example verdict must be allow or deny')
        mutation_present = 'arguments' in value
        if mutation_present and (not self.pre_tool or verdict != 'allow'):
            reject_unsupported('example arguments require a BeforeTool allow')
        if mutation_present and not isinstance(value['arguments'], dict):
            reject_unsupported('example arguments must be an object')
        decision = cast('str | None', verdict)
        if not self.pre_tool and decision == 'allow':
            decision = None
        return Contribution(
            decision='deny' if decision == 'deny' else decision,
            decision_reason=optional_string(value, 'reason'),
            mutation=value.get('arguments'),
            mutation_present=mutation_present,
            system_message=optional_string(value, 'note'),
        )


class ExampleHost:
    api_version = API_VERSION
    events = EVENTS
    name = 'example'
    requires_event = True

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> ExampleAdapter:
        return ExampleAdapter(
            event, raw_input, payload, diagnostics, allow_on_failure=allow_on_failure
        )

    def configuration_response(
        self,
        event: str,
        reason: str,
        diagnostics: Diagnostics,
    ) -> ExampleResponse:
        del reason
        if event not in EVENTS:
            raise ValueError(f'example has no configuration response for {event!r}')
        diagnostics.write_global('example: configuration response')
        return ExampleResponse(
            b'{"verdict":"deny","reason":"sequential-hooks configuration error"}\n'
        )

    def doctor_loader(self) -> DoctorLoader:
        return ExampleLoader()

    def payload_event(self, payload: Mapping[str, object]) -> str | None:
        del payload
        return None
