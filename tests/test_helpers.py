import subprocess
import sys
from pathlib import Path

import pytest

from tests import _helpers
from tests._helpers import decode_text, run_setup


def test_decode_text_uses_universal_newlines() -> None:
    assert decode_text(b'alpha\r\nbeta\rgamma\ndelta') == 'alpha\nbeta\ngamma\ndelta'


def test_run_setup_timeout_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    real_finish = _helpers.finish_process
    children: list[subprocess.Popen[bytes]] = []

    def finish_child(
        process: subprocess.Popen[bytes],
        *,
        stdin: bytes | None,
        cwd: Path | str | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        children.append(process)
        return real_finish(process, stdin=stdin, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(_helpers, 'finish_process', finish_child)
    try:
        with pytest.raises(AssertionError, match=r'timed out after 0\.2 s'):
            run_setup(
                [sys.executable, '-c', 'import threading; threading.Event().wait(30)'], timeout=0.2
            )
        assert len(children) == 1
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=2)
