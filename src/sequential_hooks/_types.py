"""Define the runner's concrete step, result, and failure records."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ContainmentState(StrEnum):
    """Describe whether a child owns native process-tree containment.

    The values are the documented `CONTAINMENT_STATES` strings that adapters
    compare.

    Attributes:
        CONTAINED: Native process-tree containment is active.
        UNCONTAINED: The child runs through an explicitly allowed fallback
            without process-tree containment.
    """

    CONTAINED = 'contained'
    UNCONTAINED = 'uncontained'


class FailureKind(StrEnum):
    """Classify an infrastructure failure before adapter interpretation.

    The values are the documented `FAILURE_KINDS` strings that adapters
    compare.

    Attributes:
        NOT_FOUND: The executable could not be resolved or started.
        SPAWN: The operating system refused process creation.
        CRASH: The child terminated with a native crash status.
        TIMEOUT: The child exceeded its execution deadline.
        OUTPUT_LIMIT: Standard output exceeded its byte budget.
        OUTPUT_WORKER_TIMEOUT: A pipe worker did not stop within cleanup
            bounds.
        CONTAINMENT: Required process-tree containment could not be
            established.
        UNSUPPORTED_EXECUTABLE: Resolution found an unsupported executable
            shim.
    """

    NOT_FOUND = 'not_found'
    SPAWN = 'spawn'
    CRASH = 'crash'
    TIMEOUT = 'timeout'
    OUTPUT_LIMIT = 'output_limit'
    OUTPUT_WORKER_TIMEOUT = 'output_worker_timeout'
    CONTAINMENT = 'containment'
    UNSUPPORTED_EXECUTABLE = 'unsupported_executable'


@dataclass(frozen=True)
class Step:
    """Describe one child command in its configured chain position.

    Args:
        index: One-based position used in diagnostics.
        argv: Literal executable token and arguments.

    Attributes:
        index: One-based position used in diagnostics.
        argv: Literal executable token and arguments.
        name: Executable basename used in diagnostics.
    """

    index: int
    argv: tuple[str, ...]

    @property
    def name(self) -> str:
        """Return the executable basename used in diagnostics.

        Returns:
            Basename, or the original executable token when it has no basename.
        """
        basename = Path(self.argv[0]).name
        return basename or self.argv[0]


@dataclass(frozen=True)
class StepFailure:
    """Describe one bounded infrastructure failure.

    Args:
        kind: Stable infrastructure failure category.
        message: Safe user-facing failure detail.
        returncode: Child status when one was observed.

    Attributes:
        kind: Stable infrastructure failure category.
        message: Safe user-facing failure detail.
        returncode: Child status when one was observed.
    """

    kind: FailureKind
    message: str
    returncode: int | None = None


@dataclass(frozen=True)
class StepResult:
    """Carry one child's bounded process outcome to an adapter.

    Args:
        returncode: Child status when one was observed.
        stdout: Bounded child standard-output bytes.
        blocking_stderr: Bounded decoded stderr retained for native responses.
        failure: Infrastructure failure, if one occurred.
        containment: Effective process-tree containment state.

    Attributes:
        returncode: Child status when one was observed.
        stdout: Bounded child standard-output bytes.
        blocking_stderr: Bounded decoded stderr retained for native responses.
        failure: Infrastructure failure, if one occurred.
        containment: Effective process-tree containment state.
    """

    returncode: int | None
    stdout: bytes
    blocking_stderr: str
    failure: StepFailure | None = None
    containment: ContainmentState = ContainmentState.CONTAINED
