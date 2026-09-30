"""Define the structural host contract shared by every host adapter package."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from sequential_hooks.hosts._records import (
    ConfigSource,
    Finding,
    InspectedCommand,
    Registration,
)

API_VERSION = 1
CONTAINMENT_STATES = frozenset({'contained', 'uncontained'})
FAILURE_KINDS = frozenset(
    {
        'not_found',
        'spawn',
        'crash',
        'timeout',
        'output_limit',
        'output_worker_timeout',
        'containment',
        'unsupported_executable',
    }
)
type ChainControl = Literal['continue', 'stop']
type JsonValue = bool | int | float | str | list['JsonValue'] | dict[str, 'JsonValue'] | None
type JsonObject = dict[str, JsonValue]


class HostInputError(ValueError):
    """Report an expected native-input rejection whose message is safe to surface.

    A host raises it from `HostSpec.adapter` when the payload is not valid
    native input for the event. Any other exception from that call is an
    adapter malfunction and reaches the wrapper's last resort.
    """


class Step(Protocol):
    """Describe one child command in its configured chain position.

    The runner constructs steps; adapters only read them.
    """

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the literal executable token and arguments.

        Returns:
            Argument vector exactly as configured.
        """
        ...

    @property
    def index(self) -> int:
        """Return the one-based chain position used in diagnostics.

        Returns:
            Position starting at 1.
        """
        ...

    @property
    def name(self) -> str:
        """Return the executable basename used in diagnostics.

        Returns:
            Basename, or the original token when it has no basename.
        """
        ...


class StepFailure(Protocol):
    """Describe one bounded infrastructure failure.

    `kind` is one of `FAILURE_KINDS`; adapters compare it as a string.
    """

    @property
    def kind(self) -> str:
        """Return the stable failure category.

        Returns:
            One of the documented `FAILURE_KINDS` values.
        """
        ...

    @property
    def message(self) -> str:
        """Return the safe user-facing failure detail.

        Returns:
            Bounded detail text.
        """
        ...

    @property
    def returncode(self) -> int | None:
        """Return the child status when one was observed.

        Returns:
            Child status, or `None`.
        """
        ...


class StepResult(Protocol):
    """Carry one child's bounded process outcome to an adapter.

    `containment` is one of `CONTAINMENT_STATES`; adapters compare it as a
    string.
    """

    @property
    def blocking_stderr(self) -> str:
        """Return bounded decoded stderr retained for native responses.

        Returns:
            Replacement-decoded child stderr text.
        """
        ...

    @property
    def containment(self) -> str:
        """Return the effective process-tree containment state.

        Returns:
            One of the documented `CONTAINMENT_STATES` values.
        """
        ...

    @property
    def failure(self) -> StepFailure | None:
        """Return the infrastructure failure, if one occurred.

        Returns:
            Failure record, or `None` when the child completed.
        """
        ...

    @property
    def returncode(self) -> int | None:
        """Return the child status when one was observed.

        Returns:
            Child status, or `None`.
        """
        ...

    @property
    def stdout(self) -> bytes:
        """Return bounded child standard-output bytes.

        Returns:
            Raw stdout, at most the documented stdout limit.
        """
        ...


class FinalResponse(Protocol):
    """Carry the wrapper's final native response and exit status."""

    @property
    def exit_code(self) -> int:
        """Return the wrapper process exit status.

        Returns:
            Exit status the wrapper returns after writing `stdout`.
        """
        ...

    @property
    def stdout(self) -> bytes:
        """Return the exact native response bytes.

        Returns:
            Bytes written unchanged to the wrapper's standard output.
        """
        ...


class Diagnostics(Protocol):
    """Write bounded, labeled developer diagnostics."""

    def write_global(self, message: str) -> None:
        """Write a diagnostic that does not belong to one step.

        Args:
            message: Safe user-facing diagnostic text.
        """
        ...

    def write_step(self, step: Step, message: str) -> None:
        """Write one labeled step diagnostic.

        Args:
            step: Child associated with the message.
            message: Safe user-facing diagnostic text.
        """
        ...


class HostAdapter(Protocol):
    """Compose ordered child results for one validated native invocation.

    Before each child, the runner obtains the current native input through
    `step_input()`. It passes each result to `consume()` in order, stops
    immediately when `consume()` returns `'stop'`, and then calls `finalize()`
    for the terminal response; otherwise it calls `finalize()` after the
    configured steps are exhausted. `should_run_steps` may bypass the child
    chain and finalize immediately.
    """

    def consume(self, step: Step, result: StepResult) -> ChainControl:
        """Apply one child result and choose whether the runner continues.

        Args:
            step: Child that produced `result`.
            result: Bounded process outcome to interpret.

        Returns:
            `'continue'` to start the next step, `'stop'` to finalize now.
        """
        ...

    @property
    def event(self) -> str:
        """Return the validated native event.

        Returns:
            One of the host's `events`.
        """
        ...

    def finalize(self) -> FinalResponse:
        """Return the final native host response.

        Returns:
            Exactly one host response and wrapper exit status.
        """
        ...

    @property
    def should_run_steps(self) -> bool:
        """Return whether this invocation should execute child steps.

        Returns:
            `True` when the runner should execute the configured chain.
        """
        ...

    def step_input(self) -> bytes:
        """Return native JSON input for the next child.

        Returns:
            Current native payload as strict JSON bytes.
        """
        ...


class DoctorLoader(Protocol):
    """Inspect one host's hook configuration without executing anything."""

    def analyze_registration(
        self,
        registration: Registration,
        inspected: InspectedCommand,
        *,
        windows: bool,
    ) -> tuple[Finding, ...]:
        """Return host-specific findings for one inspected registration.

        Args:
            registration: Accepted registration.
            inspected: Static inspection of the registration's command.
            windows: Whether Windows launch semantics apply.

        Returns:
            Findings in report order; empty when the host adds none.
        """
        ...

    def discover_sources(
        self,
        home: Path,
        cwd: Path,
        system_root: Path,
    ) -> tuple[ConfigSource, ...]:
        """Return every configuration source in deterministic inspection order.

        Args:
            home: Home directory used to locate user configuration.
            cwd: Working directory used to locate project configuration.
            system_root: Filesystem root used to locate system configuration.

        Returns:
            Candidate sources, existing or not, in inspection order.
        """
        ...

    def inspection_limitations(self) -> tuple[str, ...]:
        """Return configuration sources static inspection cannot establish.

        Returns:
            Stable limitation descriptions in report order.
        """
        ...

    def load_registrations(
        self,
        sources: tuple[ConfigSource, ...],
    ) -> tuple[tuple[Registration, ...], tuple[Finding, ...]]:
        """Extract registrations and structural findings in source order.

        Args:
            sources: Sources returned by `discover_sources`.

        Returns:
            Accepted registrations and source-isolated findings.
        """
        ...


class HostSpec(Protocol):
    """Describe one host: its events, adapter factory, and doctor loader.

    An entry point in the `sequential_hooks.hosts` group names a zero-argument
    callable, normally a class, that constructs an object satisfying this
    Protocol. `api_version` must equal `API_VERSION`.
    """

    def adapter(
        self,
        event: str,
        raw_input: bytes,
        payload: JsonObject,
        diagnostics: Diagnostics,
        *,
        allow_on_failure: bool,
    ) -> HostAdapter:
        """Validate native input and construct the adapter for one invocation.

        Args:
            event: Trusted event, one of `events`.
            raw_input: Original native strict JSON hook input.
            payload: Parsed native strict JSON object.
            diagnostics: Bounded developer diagnostic sink.
            allow_on_failure: Whether approved pre-tool failures may continue.

        Returns:
            Adapter ready to drive the chain.

        Raises:
            HostInputError: If the payload is not valid input for `event`.
        """
        ...

    @property
    def api_version(self) -> int:
        """Return the contract version this host implements.

        Returns:
            The `API_VERSION` the host was written against.
        """
        ...

    def configuration_response(
        self,
        event: str,
        reason: str,
        diagnostics: Diagnostics,
    ) -> FinalResponse:
        """Return the host's safe fail-closed answer to a configuration error.

        Args:
            event: Trusted event, one of `events`.
            reason: Wrapper-owned diagnostic published after response
                resolution and omitted from native response.
            diagnostics: Bounded developer diagnostic sink.

        Returns:
            Native response computable from the host and event alone.
        """
        ...

    def doctor_loader(self) -> DoctorLoader:
        """Return the loader that inspects this host's configuration.

        Returns:
            Static configuration loader for `doctor`.
        """
        ...

    @property
    def events(self) -> Sequence[str]:
        """Return the native events this host serves.

        Returns:
            Event names accepted by `adapter` and `configuration_response`.
        """
        ...

    @property
    def name(self) -> str:
        """Return the host name selected by `--agent`.

        Returns:
            Entry-point name.
        """
        ...

    def payload_event(self, payload: Mapping[str, object]) -> str | None:
        """Return the event a payload carries, for hosts that do not require `--event`.

        Args:
            payload: Parsed native strict JSON object.

        Returns:
            One of `events`, or `None` when the payload carries no recognizable
            event.
        """
        ...

    @property
    def requires_event(self) -> bool:
        """Return whether the host payload carries no event name.

        Returns:
            `True` when `--event` is required and `payload_event` is unused.
        """
        ...
