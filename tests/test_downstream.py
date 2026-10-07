import errno
import io
import os

import pytest

from sequential_hooks import _downstream
from sequential_hooks._downstream import (
    is_downstream_closure,
    silence_descriptor,
)


class _DescriptorStream(io.StringIO):
    def fileno(self) -> int:
        return 1


class _FakeOs:
    O_WRONLY = os.O_WRONLY
    devnull = os.devnull

    def __init__(self, *, redirect_error: OSError | None) -> None:
        self.events: list[tuple[str, int]] = []
        self.redirect_error = redirect_error

    def close(self, fd: int) -> None:
        self.events.append(('close', fd))

    def dup2(self, source: int, _target: int) -> None:
        self.events.append(('dup2', source))
        if self.redirect_error is not None:
            raise self.redirect_error

    def open(self, _path: str, _flags: int) -> int:
        return 37


@pytest.mark.parametrize('windows', [False, True])
def test_broken_pipe_is_closure_everywhere(windows: bool) -> None:
    assert is_downstream_closure(BrokenPipeError(), windows=windows)


def test_einval_is_closure_only_on_windows() -> None:
    error = OSError(errno.EINVAL, 'Invalid argument')
    assert is_downstream_closure(error, windows=True)
    assert not is_downstream_closure(error, windows=False)


def test_silence_descriptor_closes_devnull_and_propagates_redirect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=OSError('injected redirect failure'))
    monkeypatch.setattr(_downstream, 'os', fake)

    with pytest.raises(OSError, match='injected redirect failure'):
        silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_silence_descriptor_ignores_streams_without_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)

    silence_descriptor(io.StringIO())
    assert fake.events == []


def test_silence_descriptor_redirects_a_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)

    silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_unrelated_errno_is_not_closure() -> None:
    assert not is_downstream_closure(OSError(errno.EBADF, 'bad'), windows=True)
