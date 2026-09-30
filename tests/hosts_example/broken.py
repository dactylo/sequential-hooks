# Deliberately defective hosts that exercise the last-resort tier.

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.hosts_example.host import ExampleAdapter, ExampleHost

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sequential_hooks.hosts import ChainControl, Diagnostics, JsonObject, Step, StepResult
    from tests.hosts_example.host import ExampleResponse


class ExplodingHost(ExampleHost):
    name = 'exploding'

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> ExampleAdapter:
        del event, raw_input, payload, diagnostics, allow_on_failure
        raise RuntimeError('adapter malfunction')


class OutdatedHost(ExampleHost):
    api_version = 0
    name = 'outdated'


class RuntimeFailureAdapter(ExampleAdapter):
    _failure_callback = ''

    def consume(self, step: Step, result: StepResult) -> ChainControl:
        if self._failure_callback == 'consume':
            raise RuntimeError('consume malfunction')
        return super().consume(step, result)

    def finalize(self) -> ExampleResponse:
        if self._failure_callback == 'finalize':
            raise RuntimeError('finalize malfunction')
        return super().finalize()

    @property
    def should_run_steps(self) -> bool:
        if self._failure_callback == 'should_run_steps':
            raise RuntimeError('should_run_steps malfunction')
        return super().should_run_steps

    def step_input(self) -> bytes:
        if self._failure_callback == 'step_input':
            raise RuntimeError('step_input malfunction')
        return super().step_input()


class RuntimeFailureHost(ExampleHost):
    failure_callback = ''

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> ExampleAdapter:
        adapter = RuntimeFailureAdapter(
            event,
            raw_input,
            payload,
            diagnostics,
            allow_on_failure=allow_on_failure,
        )
        adapter._failure_callback = self.failure_callback
        return adapter


class RuntimeConsumeFailureHost(RuntimeFailureHost):
    failure_callback = 'consume'
    name = 'runtime-consume-failure'


class RuntimeFinalizeFailureHost(RuntimeFailureHost):
    failure_callback = 'finalize'
    name = 'runtime-finalize-failure'


class RuntimeShouldRunFailureHost(RuntimeFailureHost):
    failure_callback = 'should_run_steps'
    name = 'runtime-should-run-failure'


class RuntimeStepInputFailureHost(RuntimeFailureHost):
    failure_callback = 'step_input'
    name = 'runtime-step-input-failure'


class UnansweringHost(ExampleHost):
    name = 'unanswering'

    def configuration_response(
        self,
        event: str,
        reason: str,
        diagnostics: Diagnostics,
    ) -> ExampleResponse:
        del event, reason, diagnostics
        raise RuntimeError('no configuration response')


class UnconstructibleHost(ExampleHost):
    name = 'unconstructible'

    def __init__(self) -> None:
        raise RuntimeError('cannot construct')


class UnresolvableEventHost(ExampleHost):
    name = 'unresolvable-event'
    requires_event = False

    def payload_event(self, payload: Mapping[str, object]) -> str | None:
        del payload
        raise RuntimeError('event callback malfunction')
