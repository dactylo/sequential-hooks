"""Retain cleanup failures while attempting every cleanup action."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from typing import Self


class CleanupState:
    """Retain the first failure while attempting every cleanup action.

    Each instance represents one cleanup pass. Entering returns the same
    cleanup state and resets only the current action result. The context
    manager retains and suppresses ordinary exceptions so later actions can
    run, but it does not suppress other `BaseException` subclasses. Create a
    new instance for each new pass.

    Attributes:
        first_error: First ordinary exception recorded during the pass, or the
            supplied earlier failure.
    """

    def __init__(self, first_error: Exception | None = None) -> None:
        """Begin one cleanup pass with an optional earlier failure.

        Args:
            first_error: Failure already selected before this cleanup pass, if
                any.
        """
        self._attempt_failed = False
        self.first_error = first_error

    def __enter__(self) -> Self:
        """Begin one guarded cleanup action.

        Returns:
            This cleanup state.
        """
        self._attempt_failed = False
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        """Retain and suppress one ordinary cleanup exception.

        Args:
            _error_type: Raised exception type, if any.
            error: Raised exception, if any.
            _traceback: Raised exception traceback, if any.

        Returns:
            Whether the raised exception was retained and suppressed.
        """
        if isinstance(error, Exception):
            self._attempt_failed = True
            self.record(error)
            return True
        return False

    def attempt(self, action: Callable[[], object]) -> bool:
        """Run one cleanup action and report whether it succeeded.

        Args:
            action: Cleanup action to attempt.

        Returns:
            Whether the action completed without an ordinary exception.
        """
        with self:
            action()
        return not self._attempt_failed

    def record(self, error: Exception | None) -> None:
        """Retain one failure only when none occurred earlier.

        Args:
            error: Ordinary failure to retain, if any.
        """
        if self.first_error is None and error is not None:
            self.first_error = error
