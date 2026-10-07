"""Define the doctor record types a host loader produces and reads."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class ContractError(StrEnum):
    """Classify a statically recognized wrapper contract failure.

    Attributes:
        EVENT_FORBIDDEN: The wrapper supplies an event to a host that does not
            accept the option.
        EVENT_MISSING: The wrapper omits the event required by its host.
        HOST_MISSING: The wrapper omits its required host.
        WRONG_EVENT: The wrapper event is unsupported or differs from the
            registered event.
        WRONG_HOST: The wrapper host is unsupported or differs from the
            configuration host.
    """

    EVENT_FORBIDDEN = 'event_forbidden'
    EVENT_MISSING = 'event_missing'
    HOST_MISSING = 'host_missing'
    WRONG_EVENT = 'wrong_event'
    WRONG_HOST = 'wrong_host'


class FindingSeverity(StrEnum):
    """Classify a doctor finding's effect on the process status.

    Attributes:
        ERROR: An actionable error that makes the doctor status nonzero.
        WARNING: An actionable warning that makes the doctor status nonzero.
        INFO: Informational context that does not make the status nonzero.
    """

    ERROR = 'error'
    WARNING = 'warning'
    INFO = 'info'


@dataclass(frozen=True)
class Finding:
    """Describe one immutable doctor finding.

    Args:
        severity: Effect of the finding on the doctor status.
        code: Machine-readable finding category.
        message: User-facing diagnostic detail.
        correction: Exact corrective guidance, when available.

    Attributes:
        severity: Effect of the finding on the doctor status.
        code: Machine-readable finding category.
        message: User-facing diagnostic detail.
        correction: Exact corrective guidance, when available.
    """

    severity: FindingSeverity
    code: str
    message: str
    correction: str | None = None


class LaunchSemantics(StrEnum):
    """Classify how a host launches a registration command.

    Attributes:
        DIRECT_ARGV: The host launches a command with an explicit argument
            vector.
        POSIX_SHELL: A POSIX shell interprets the command text.
        WINDOWS_SHELL: A Windows shell interprets the command text.
        HOST_TOKENIZER: The host owns platform-specific command tokenization.
        UNKNOWN: No supported launch behavior is known.
    """

    DIRECT_ARGV = 'direct_argv'
    POSIX_SHELL = 'posix_shell'
    WINDOWS_SHELL = 'windows_shell'
    HOST_TOKENIZER = 'host_tokenizer'
    UNKNOWN = 'unknown'


class SourceKind(StrEnum):
    """Classify the ownership scope of a configuration source.

    Attributes:
        USER: User-scoped configuration.
        PROJECT: Project-scoped configuration inherited by the working
            directory.
        SYSTEM: System-wide configuration.
        MANAGED: Organization-managed configuration.
        LOCAL: Project-local override configuration.
    """

    USER = 'user'
    PROJECT = 'project'
    SYSTEM = 'system'
    MANAGED = 'managed'
    LOCAL = 'local'


@dataclass(frozen=True)
class ConfigSource:
    """Describe one immutable candidate configuration source.

    Args:
        path: Candidate configuration path.
        kind: Ownership scope assigned by the host loader.
        exists: Whether the candidate existed during discovery.

    Attributes:
        path: Candidate configuration path.
        kind: Ownership scope assigned by the host loader.
        exists: Whether the candidate existed during discovery.
    """

    path: Path
    kind: SourceKind
    exists: bool


@dataclass(frozen=True)
class Registration:
    """Describe one immutable host registration accepted by a loader.

    Args:
        source: Configuration source that owns the registration.
        event: Native host event containing the registration.
        matcher: Native host matcher, when configured.
        command: Primary command text or executable token.
        args: Explicit argument vector tail, when the host provides one.
        launch_semantics: Launch behavior for the primary command.
        command_windows: Windows-specific command text, when configured.
        windows_launch_semantics: Windows launch behavior, when documented.
        timeout: Outer host timeout, when valid and configured.
        enabled: Statically known activation state, when available.
        type: Native handler type, when available.
        configuration_findings: Notes from accepting this hook, in loader
            order.

    Attributes:
        source: Configuration source that owns the registration.
        event: Native host event containing the registration.
        matcher: Native host matcher, when configured.
        command: Primary command text or executable token.
        args: Explicit argument vector tail, when the host provides one.
        launch_semantics: Launch behavior for the primary command.
        command_windows: Windows-specific command text, when configured.
        windows_launch_semantics: Windows launch behavior, when documented.
        timeout: Outer host timeout, when valid and configured.
        enabled: Statically known activation state, when available.
        type: Native handler type, when available.
        configuration_findings: Notes from accepting this hook, in loader
            order.
    """

    source: ConfigSource
    event: str
    matcher: str | None
    command: str
    args: tuple[str, ...] | None
    launch_semantics: LaunchSemantics
    command_windows: str | None
    windows_launch_semantics: LaunchSemantics | None
    timeout: float | None
    enabled: bool | None
    type: str | None
    configuration_findings: tuple[Finding, ...] = ()


class WrapperStep(Protocol):
    """Describe one child command parsed from a registered wrapper command."""

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the literal executable token and arguments.

        Returns:
            Argument vector exactly as registered.
        """
        ...

    @property
    def index(self) -> int:
        """Return the one-based chain position.

        Returns:
            Position starting at 1.
        """
        ...


class WrapperInvocation(Protocol):
    """Describe one statically parsed wrapper command from a registration."""

    @property
    def allow_on_failure(self) -> bool:
        """Return whether the registration passes `--allow-on-failure`.

        Returns:
            `True` when the failure override is configured.
        """
        ...

    @property
    def event(self) -> str | None:
        """Return the explicit `--event` value, if any.

        Returns:
            Event name, or `None` when the command passes no event.
        """
        ...

    @property
    def host(self) -> str:
        """Return the `--agent` value.

        Returns:
            Host name exactly as registered.
        """
        ...

    @property
    def step_timeout(self) -> float | None:
        """Return the `--step-timeout` value, if any.

        Returns:
            Positive finite seconds, or `None`.
        """
        ...

    @property
    def steps(self) -> Sequence[WrapperStep]:
        """Return the child commands in execution order.

        Returns:
            One to three steps.
        """
        ...


@dataclass(frozen=True)
class InspectedCommand:
    """Carry one immutable, non-executing command inspection.

    Args:
        argv: Statically decoded argument vector, when available.
        invocation: Parsed wrapper invocation, when the command invokes the
            wrapper and satisfies its grammar.
        dynamic: Whether shell syntax prevents static inspection.
        error: Bounded reason static inspection did not yield a usable command,
            when applicable.
        contract_error: Safely recognized wrapper contract failure, when
            applicable.

    Attributes:
        argv: Statically decoded argument vector, when available.
        invocation: Parsed wrapper invocation, when available.
        dynamic: Whether shell syntax prevents static inspection.
        error: Bounded static-inspection failure detail, when applicable.
        contract_error: Safely recognized wrapper contract failure, when
            applicable.
    """

    argv: tuple[str, ...] | None
    invocation: WrapperInvocation | None
    dynamic: bool
    error: str | None
    contract_error: ContractError | None = None
