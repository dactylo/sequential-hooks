"""Contain child process trees in POSIX process groups."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, BinaryIO, cast

from sequential_hooks._containment import ContainmentError, start_process
from sequential_hooks._containment._uncontained import UncontainedBackend
from sequential_hooks._types import ContainmentState

if TYPE_CHECKING:
    from sequential_hooks._containment import ManagedStep, StderrReader
    from sequential_hooks._types import Step

_ABORT_GRACE_SECONDS = 1.0
_FORCE_KILL_WAIT_SECONDS = 1.0


class PosixBackend:
    """Create process-group-contained POSIX steps."""

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        """Return an explicit uncontained fallback after release failure.

        Args:
            step: Step whose prepared process could not be released.
            failed: Failed managed process already disposed by the executor.
            allow_uncontained: Whether the user explicitly allowed fallback.

        Returns:
            Fresh explicitly uncontained process.

        Raises:
            ContainmentError: If fallback was not explicitly allowed.
        """
        del failed
        if not allow_uncontained:
            raise ContainmentError('containment release failed without explicit override')
        return UncontainedBackend().start(step, allow_uncontained=True)

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        """Create one process-group-contained POSIX process.

        Args:
            step: Literal argv to start.
            allow_uncontained: Accepted for the backend interface; this method
                does not use it because fallback is handled separately.

        Returns:
            Managed POSIX process group.
        """
        del allow_uncontained
        return _PosixManagedStep(step)


class _PosixManagedStep:
    """Own one POSIX process group and its parent pipe handles."""

    def __init__(self, step: Step) -> None:
        """Start one contained POSIX child and validate its pipes."""
        self._graceful_signum: int | None = None
        self._process = start_process(step, contained=True)
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            self.terminate_tree(_ABORT_GRACE_SECONDS)
            raise ContainmentError('managed POSIX child pipes were not created')

    def _group_exists(self) -> bool:
        """Return whether the exact managed process group still exists."""
        if sys.platform == 'win32':
            raise ContainmentError('POSIX process groups are unavailable on Windows')
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_for_group_exit(self, deadline: float) -> bool:
        """Wait only until one absolute deadline for the exact group to vanish."""
        while self._group_exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=min(0.01, remaining))
            else:
                time.sleep(min(0.01, remaining))
        return True

    def _wait_for_process_reap(self, deadline: float) -> bool:
        """Wait only until one absolute deadline for the leader to be reaped."""
        if self._process.poll() is not None:
            return True
        try:
            self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        return True

    def _signal_group(self, signum: int, deadline: float) -> None:
        """Send one signal only to the exact managed process group."""
        if sys.platform == 'win32':
            raise ContainmentError('POSIX process groups are unavailable on Windows')
        try:
            os.killpg(self._process.pid, signum)
        except ProcessLookupError:
            return
        except PermissionError:
            # Darwin can report EPERM while an exiting leader is not yet waitable.
            if not self._wait_for_process_reap(deadline):
                raise
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signum)

    @property
    def containment(self) -> ContainmentState:
        """Return the active whole-tree containment state.

        Returns:
            Contained state for this POSIX process group.
        """
        return ContainmentState.CONTAINED

    def forward_signal(self, signum: int) -> None:
        """Forward and record the wrapper's actual graceful signal.

        Exceptional Darwin exit-race recovery may wait up to the force-kill
        bound before returning.

        Args:
            signum: Actual signal received by the wrapper.
        """
        if self._graceful_signum is None:
            self._graceful_signum = signum
            deadline = time.monotonic() + _FORCE_KILL_WAIT_SECONDS
            self._signal_group(signum, deadline)

    @property
    def process(self) -> subprocess.Popen[bytes]:
        """Return the immediate process completion handle.

        Returns:
            Immediate POSIX child process.
        """
        return self._process

    def release(self) -> None:
        """Do nothing because the POSIX child starts during construction."""

    @property
    def stderr(self) -> StderrReader:
        """Return the parent stderr pipe.

        Returns:
            Binary stderr reader.
        """
        return cast('StderrReader', self._process.stderr)

    @property
    def stdin(self) -> BinaryIO:
        """Return the parent stdin pipe.

        Returns:
            Binary stdin writer.
        """
        return cast('BinaryIO', self._process.stdin)

    @property
    def stdout(self) -> BinaryIO:
        """Return the parent stdout pipe.

        Returns:
            Binary stdout reader.
        """
        return cast('BinaryIO', self._process.stdout)

    def terminate_tree(self, grace_seconds: float) -> None:
        """Terminate the exact managed group after one grace period.

        Args:
            grace_seconds: Maximum graceful wait before `SIGKILL`.

        Raises:
            ContainmentError: If the process group does not disappear or its
                leader cannot be reaped within the bounded cleanup waits.
        """
        if sys.platform == 'win32':
            raise ContainmentError('POSIX process groups are unavailable on Windows')
        graceful_deadline = time.monotonic() + max(0.0, grace_seconds)
        if self._graceful_signum is None:
            self._graceful_signum = signal.SIGTERM
            self._signal_group(signal.SIGTERM, graceful_deadline)

        if self._wait_for_group_exit(graceful_deadline):
            reap_deadline = time.monotonic() + _FORCE_KILL_WAIT_SECONDS
            if not self._wait_for_process_reap(reap_deadline):
                raise ContainmentError('managed POSIX leader could not be reaped')
            return

        force_deadline = time.monotonic() + _FORCE_KILL_WAIT_SECONDS
        self._signal_group(signal.SIGKILL, force_deadline)
        group_disappeared = self._wait_for_group_exit(force_deadline)
        process_reaped = self._wait_for_process_reap(force_deadline)
        if not group_disappeared:
            raise ContainmentError('managed POSIX process group did not disappear after SIGKILL')
        if not process_reaped:
            raise ContainmentError('managed POSIX leader could not be reaped after SIGKILL')

    def abort_before_release(self) -> None:
        """Dispose of the POSIX child process.

        POSIX has no release gate, so aborting is ordinary tree termination.
        """
        self.terminate_tree(_ABORT_GRACE_SECONDS)

    def finish(self) -> None:
        """Terminate remaining group members after immediate completion."""
        self.terminate_tree(_ABORT_GRACE_SECONDS)
