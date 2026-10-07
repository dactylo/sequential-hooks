"""Run explicitly allowed children without process-tree containment."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING, BinaryIO, cast

from sequential_hooks._containment import ContainmentError, start_process
from sequential_hooks._types import ContainmentState

if TYPE_CHECKING:
    from sequential_hooks._containment import ManagedStep, StderrReader
    from sequential_hooks._containment._windows_api import WindowsApi
    from sequential_hooks._types import Step

_ABORT_GRACE_SECONDS = 1.0
_CREATE_NEW_PROCESS_GROUP = cast(
    'int',
    getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200),
)
_FORCE_KILL_WAIT_SECONDS = 1.0


class UncontainedBackend:
    """Create only explicitly authorized uncontained processes."""

    def start(self, step: Step, *, allow_uncontained: bool) -> ManagedStep:
        """Create one explicitly authorized uncontained process.

        Args:
            step: Literal argv to start.
            allow_uncontained: Whether the user explicitly allowed fallback.

        Returns:
            Fresh uncontained immediate process.

        Raises:
            ContainmentError: If fallback was not explicitly allowed.
            OSError: If starting the process fails.
        """
        if not allow_uncontained:
            raise ContainmentError('uncontained execution requires an explicit override')
        return _UncontainedManagedStep(step)

    def recover_release_failure(
        self,
        step: Step,
        failed: ManagedStep,
        *,
        allow_uncontained: bool,
    ) -> ManagedStep:
        """Return a fresh explicitly uncontained process.

        Args:
            step: Step whose prepared process could not be released.
            failed: Failed managed process already disposed by the executor.
            allow_uncontained: Whether the user explicitly allowed fallback.

        Returns:
            Fresh uncontained immediate process.

        Raises:
            ContainmentError: If fallback was not explicitly allowed.
            OSError: If starting the process fails.
        """
        del failed
        return self.start(step, allow_uncontained=allow_uncontained)


class _UncontainedManagedStep:
    """Own only one explicitly uncontained immediate process."""

    def __init__(
        self,
        step: Step,
        *,
        platform_name: str | None = None,
        windows_api: WindowsApi | None = None,
    ) -> None:
        """Start one explicitly uncontained child and validate its pipes."""
        selected_platform = sys.platform if platform_name is None else platform_name
        self._graceful_signal_forwarded = False
        self._windows_api = None
        if selected_platform == 'win32':
            if windows_api is None:
                from sequential_hooks._containment._windows_api import (  # noqa: PLC0415
                    NativeWindowsApi,
                )

                windows_api = NativeWindowsApi()
            self._windows_api = windows_api
        creationflags = _CREATE_NEW_PROCESS_GROUP if self._windows_api is not None else 0
        self._process = start_process(
            step,
            contained=False,
            creationflags=creationflags,
        )
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            self.terminate_tree(_ABORT_GRACE_SECONDS)
            raise ContainmentError('uncontained child pipes were not created')

    def _terminate_windows(self, api: WindowsApi, grace_seconds: float) -> None:
        """Reap the owned native process within the grace and force deadlines."""
        grace_deadline = time.monotonic() + max(0.0, grace_seconds)
        exited = self._process.poll() is not None
        termination_requested = False
        try:
            if not exited and not self._graceful_signal_forwarded:
                with contextlib.suppress(ProcessLookupError):
                    self._process.terminate()
                termination_requested = True
            if api.wait_process(self._process, max(0.0, grace_deadline - time.monotonic())):
                self._process.wait(timeout=0.0)
                return
            force_deadline = time.monotonic() + _FORCE_KILL_WAIT_SECONDS
            if not exited and not termination_requested:
                self._process.kill()
            if not api.wait_process(self._process, max(0.0, force_deadline - time.monotonic())):
                raise ContainmentError(
                    'uncontained immediate process could not be reaped after kill'
                )
            self._process.wait(timeout=max(0.0, force_deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContainmentError(
                'uncontained immediate process could not be reaped after kill'
            ) from error

    @property
    def containment(self) -> ContainmentState:
        """Return the reduced containment state.

        Returns:
            Explicitly uncontained state.
        """
        return ContainmentState.UNCONTAINED

    def forward_signal(self, signum: int) -> None:
        """Forward one graceful signal to the immediate process.

        Args:
            signum: Actual signal received by the wrapper.
        """
        if self._graceful_signal_forwarded or self._process.poll() is not None:
            return
        if self._windows_api is not None:
            del signum
            try:
                delivered = self._windows_api.generate_ctrl_break(self._process.pid)
            except OSError:
                return
            if not delivered:
                return
        else:
            try:
                self._process.send_signal(signum)
            except ProcessLookupError:
                return
        self._graceful_signal_forwarded = True

    @property
    def process(self) -> subprocess.Popen[bytes]:
        """Return the immediate process completion handle.

        Returns:
            Immediate uncontained process.
        """
        return self._process

    def release(self) -> None:
        """Do nothing because the uncontained child starts during construction."""

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
        """Terminate only the immediate process after a bounded grace.

        Args:
            grace_seconds: Maximum graceful wait before force termination.

        Raises:
            ContainmentError: If the immediate process cannot be reaped after
                forced termination.
        """
        if self._windows_api is not None:
            self._terminate_windows(self._windows_api, grace_seconds)
            return
        if self._process.poll() is not None:
            return
        if not self._graceful_signal_forwarded:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
        try:
            self._process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            self._process.kill()
            try:
                self._process.wait(timeout=_FORCE_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise ContainmentError(
                    'uncontained immediate process could not be reaped after kill'
                ) from error

    def abort_before_release(self) -> None:
        """Dispose of the immediate process before release."""
        self.terminate_tree(_ABORT_GRACE_SECONDS)

    def finish(self) -> None:
        """Terminate only the remaining immediate process."""
        self.terminate_tree(_ABORT_GRACE_SECONDS)
