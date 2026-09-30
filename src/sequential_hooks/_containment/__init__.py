"""Select and define native process-tree containment boundaries."""

from __future__ import annotations

import platform
import subprocess
import sys
from enum import StrEnum
from typing import TYPE_CHECKING, BinaryIO, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sequential_hooks._types import ContainmentState, Step, StepFailure


@runtime_checkable
class CompletionFailureProvider(Protocol):
    """Expose a platform failure decoded after process completion.

    After monitoring, the executor reads `completion_failure` to distinguish a
    decoded platform failure from the process return code.
    """

    @property
    def completion_failure(self) -> StepFailure | None:
        """Return a platform completion failure decoded after process exit.

        Returns:
            Decoded completion failure, or `None` when no such failure was
            decoded. `None` does not establish that the child exited
            successfully.
        """
        ...


@runtime_checkable
class CompletionProcess(Protocol):
    """Observe the immediate process used as a managed completion handle.

    `poll()` and `wait()` may resolve platform status before returning a
    result. `returncode` remains `None` until completion has been observed.
    Implementations surface their documented operating-system and protocol
    failures to the caller.
    """

    def poll(self) -> int | None:
        """Return the child status without blocking.

        Returns:
            Child status, or `None` while the process is running. The Windows
            launcher returns `0` for a decoded real-step launch failure;
            inspect the managed step's `completion_failure` to distinguish that
            result from a successful child exit.

        Raises:
            OSError: If process or status-channel observation fails.
            Implementation-specific exception: If malformed completion data
                violates the concrete platform's protocol or invariants.
        """
        ...

    @property
    def returncode(self) -> int | None:
        """Return the observed child status when available.

        Returns:
            Child status, or `None` before completion is observed or when the
            Windows launcher reports a real-step launch failure.
        """
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for completion and return the child status.

        Args:
            timeout: Maximum seconds to wait, or `None` to wait indefinitely.

        Returns:
            Child status after completion. The Windows launcher returns `0` for
            a decoded real-step launch failure; inspect the managed step's
            `completion_failure` to distinguish that result from a successful
            child exit.

        Raises:
            OSError: If process or status-channel observation fails.
            subprocess.TimeoutExpired: If the process remains running after
                `timeout`.
            Implementation-specific exception: If malformed completion data
                violates the concrete platform's protocol or invariants.
        """
        ...


class ContainmentError(RuntimeError):
    """Report that a managed process could not be established."""


class ContainmentPlatform(StrEnum):
    """Name one exact containment implementation classification.

    Attributes:
        POSIX: POSIX process-group containment.
        UNAVAILABLE: Unsupported runtime classification with value
            `uncontained`.
        WINDOWS: Windows Job Object containment.
    """

    POSIX = 'posix'
    UNAVAILABLE = 'uncontained'
    WINDOWS = 'windows'


class ContainmentReleaseError(RuntimeError):
    """Report failure while releasing a prepared process."""


class StderrReader(Protocol):
    """Read and close one buffered native stderr pipe."""

    def close(self) -> None:
        """Close the pipe."""
        ...

    def read1(self, size: int = -1, /) -> bytes:
        """Read at most one buffered chunk.

        Args:
            size: Maximum bytes to read, or `-1` for the stream default.

        Returns:
            The bytes read, or an empty value at EOF.
        """
        ...


class ManagedStep(Protocol):
    """Own one prepared step through release, monitoring, and cleanup.

    The executor acquires the standard-stream pipes before calling `release()`.
    It calls `abort_before_release()` if preparation or release fails, and
    calls `finish()` after monitoring ends. Signal and timeout paths may call
    `forward_signal()` and `terminate_tree()` first.
    """

    def abort_before_release(self) -> None:
        """Dispose of a managed step after preparation or release fails.

        Backends without a launch gate may already have started the real child.
        """
        ...

    @property
    def containment(self) -> ContainmentState:
        """Return whether whole-tree containment is active.

        Returns:
            Current containment state for the managed step.
        """
        ...

    def finish(self) -> None:
        """Terminate remaining managed processes and release containment.

        Uncontained backends can clean up only the immediate process.
        """
        ...

    def forward_signal(self, signum: int) -> None:
        """Forward a graceful signal when the platform can do so.

        Args:
            signum: Signal received by the wrapper.
        """
        ...

    @property
    def process(self) -> CompletionProcess:
        """Return the immediate process used as the completion handle.

        Returns:
            Process facade used to observe completion.
        """
        ...

    def release(self) -> None:
        """Release a prepared process after output workers are ready."""
        ...

    @property
    def stderr(self) -> StderrReader:
        """Return the pipe carrying native step stderr.

        Returns:
            Binary reader for native step stderr.
        """
        ...

    @property
    def stdin(self) -> BinaryIO:
        """Return the pipe that carries native step input.

        Returns:
            Binary writer for native step stdin.
        """
        ...

    @property
    def stdout(self) -> BinaryIO:
        """Return the pipe carrying native step stdout.

        Returns:
            Binary reader for native step stdout.
        """
        ...

    def terminate_tree(self, grace_seconds: float) -> None:
        """Terminate the managed tree and wait for bounded cleanup.

        Args:
            grace_seconds: Maximum graceful wait before forced termination.
        """
        ...


class ContainmentBackend(Protocol):
    """Create managed steps and recover an allowed release failure.

    `start()` returns a fresh process under the selected containment policy.
    After the executor disposes a step whose release gate failed,
    `recover_release_failure()` returns a distinct allowed fallback or refuses
    it with `ContainmentError`.
    """

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        """Return an allowed fallback after the failed step has been disposed.

        Args:
            step: Step whose prepared process could not be released.
            failed: Failed managed process already disposed by the executor.
            allow_uncontained: Whether the user explicitly allowed fallback.

        Returns:
            Fresh fallback process allowed by the selected backend.

        Raises:
            ContainmentError: If fallback is disallowed.
            OSError: If fallback startup fails.
        """
        ...

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        """Create one managed or explicitly uncontained process.

        Args:
            step: Literal argument vector to start.
            allow_uncontained: Whether the user explicitly allowed fallback.

        Returns:
            Fresh process under the selected containment policy.

        Raises:
            ContainmentError: If required containment cannot be established.
            OSError: If platform process or setup operations fail without
                conversion by the concrete backend.
        """
        ...


@runtime_checkable
class WindowsExitCodeProvider(Protocol):
    """Expose whether a managed result uses unsigned Windows exit codes."""

    @property
    def uses_windows_exit_codes(self) -> bool:
        """Return whether the immediate result uses unsigned Windows status.

        Returns:
            Whether the process exposes unsigned Windows exit codes.
        """
        ...


def _classify_platform(
    implementation: str,
    platform_name: str,
) -> ContainmentPlatform:
    """Classify exact native interpreter facts for containment."""
    if implementation != 'CPython':
        return ContainmentPlatform.UNAVAILABLE
    if platform_name in {'darwin', 'linux'}:
        return ContainmentPlatform.POSIX
    if platform_name == 'win32':
        return ContainmentPlatform.WINDOWS
    return ContainmentPlatform.UNAVAILABLE


def _resolve_runtime(
    *,
    implementation: str | None = None,
    platform_name: str | None = None,
) -> ContainmentPlatform:
    """Resolve the current containment platform classification."""
    selected_implementation = (
        platform.python_implementation() if implementation is None else implementation
    )
    selected_platform = sys.platform if platform_name is None else platform_name
    return _classify_platform(selected_implementation, selected_platform)


def current_backend(
    *,
    implementation: str | None = None,
    platform_name: str | None = None,
) -> ContainmentBackend:
    """Return the backend for the running native interpreter.

    Args:
        implementation: Python implementation name, or the current value.
        platform_name: Runtime platform name, or the current value.

    Returns:
        Fresh backend for the effective interpreter and platform.
    """
    runtime = _resolve_runtime(
        implementation=implementation,
        platform_name=platform_name,
    )
    if runtime is ContainmentPlatform.POSIX:
        from sequential_hooks._containment._posix import PosixBackend  # noqa: PLC0415

        return PosixBackend()
    if runtime is ContainmentPlatform.WINDOWS:
        from sequential_hooks._containment._windows import WindowsBackend  # noqa: PLC0415

        return WindowsBackend()
    from sequential_hooks._containment._uncontained import (  # noqa: PLC0415
        UncontainedBackend,
    )

    return UncontainedBackend()


def probe_current_containment(
    *,
    implementation: str | None = None,
    platform_name: str | None = None,
) -> tuple[ContainmentPlatform, bool | None, tuple[str, ...]]:
    """Report static containment availability without starting a process.

    Args:
        implementation: Python implementation name, or the current value.
        platform_name: Runtime platform name, or the current value.

    Returns:
        Platform classification, Job Object configuration result, and bounded
        limitations. The configuration result is `True` or `False` when the
        classification is `ContainmentPlatform.WINDOWS`, and `None` for other
        classifications.
    """
    containment = _resolve_runtime(
        implementation=implementation,
        platform_name=platform_name,
    )
    if containment is ContainmentPlatform.POSIX:
        return containment, None, ()
    if containment is ContainmentPlatform.WINDOWS:
        from sequential_hooks._containment._windows_api import (  # noqa: PLC0415
            probe_job_object,
        )

        configurable = probe_job_object()
        limitations = () if configurable else ('kill-on-close Job Object is unavailable',)
        return containment, configurable, limitations
    return (
        containment,
        None,
        ('whole-process-tree containment is unavailable for this interpreter',),
    )


def start_process(
    step: Step,
    *,
    contained: bool,
    creationflags: int = 0,
) -> subprocess.Popen[bytes]:
    """Start one literal argv with binary parent pipes.

    Args:
        step: Literal argv to execute.
        contained: Whether POSIX must create a new process session.
        creationflags: Native process-creation flags.

    Returns:
        Immediate process with binary standard-stream pipes.
    """
    return subprocess.Popen(  # noqa: S603
        step.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        shell=False,
        start_new_session=contained,
    )
