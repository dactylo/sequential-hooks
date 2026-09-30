from __future__ import annotations

import pytest

from sequential_hooks._cleanup import CleanupState


class _FatalCleanupError(BaseException):
    pass


def test_cleanup_state_attempt_reports_success_and_failure() -> None:
    cleanup = CleanupState()
    actions: list[str] = []

    def fail() -> None:
        raise ValueError('failed')

    assert cleanup.attempt(lambda: actions.append('success'))
    assert not cleanup.attempt(fail)

    assert actions == ['success']
    assert isinstance(cleanup.first_error, ValueError)
    assert str(cleanup.first_error) == 'failed'


def test_cleanup_state_context_propagates_base_exception() -> None:
    cleanup = CleanupState()

    with pytest.raises(_FatalCleanupError), cleanup:
        raise _FatalCleanupError

    assert cleanup.first_error is None


def test_cleanup_state_context_suppresses_ordinary_exception() -> None:
    cleanup = CleanupState()

    with cleanup:
        raise ValueError('guarded')

    assert isinstance(cleanup.first_error, ValueError)
    assert str(cleanup.first_error) == 'guarded'


def test_cleanup_state_retains_first_error_and_accepts_none() -> None:
    first = ValueError('first')
    cleanup = CleanupState(first)

    cleanup.record(None)
    cleanup.record(RuntimeError('second'))

    assert cleanup.first_error is first
