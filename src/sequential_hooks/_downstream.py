"""Recognize a closed downstream reader and silence its descriptor."""

import errno
import os
import sys
from typing import TextIO

_WINDOWS = sys.platform == 'win32'


def is_downstream_closure(error: OSError, *, windows: bool | None = None) -> bool:
    """Report whether a write or flush failed because the reader closed the stream.

    Args:
        error: Error raised by a write or flush of an output stream.
        windows: Whether Windows error reporting applies, or `None` for the
            running platform. CPython on Windows can report a write to a pipe
            whose reader closed as `EINVAL`.

    Returns:
        Whether the error means the downstream reader is gone.
    """
    on_windows = _WINDOWS if windows is None else windows
    return isinstance(error, BrokenPipeError) or (on_windows and error.errno == errno.EINVAL)


def silence_descriptor(stream: TextIO) -> None:
    """Point a stream's descriptor at the null device when it has one.

    Args:
        stream: Stream whose descriptor the interpreter flushes at shutdown.

    Raises:
        OSError: If redirecting an existing descriptor fails.
    """
    try:
        target = stream.fileno()
    except (OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, target)
    finally:
        os.close(devnull)
