import os
import subprocess
import sys
import threading
from pathlib import Path

from tests._helpers import (
    PROCESS_GROUP_FLAGS,
    _kill_and_describe,
    finish_process,
    process_transcript,
)


def test_imported_distribution_metadata_is_removed_at_process_exit(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    environment = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join((str(root), str(root / 'src'))),
        'TMPDIR': str(tmp_path),
        'TMP': str(tmp_path),
        'TEMP': str(tmp_path),
    }
    script = (
        'import sys\n'
        'from tests._distributions import _HOSTS\n'
        'print(_HOSTS, flush=True)\n'
        'sys.stdin.buffer.read()\n'
    )
    child = subprocess.Popen(
        [sys.executable, '-c', script],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    stdout = child.stdout
    assert stdout is not None
    descriptor = stdout.fileno()
    first_line = bytearray()

    def read_first_line() -> None:
        while True:
            chunk = os.read(descriptor, 65536)
            first_line.extend(chunk)
            if not chunk or b'\n' in chunk:
                return

    def readiness_transcript() -> str:
        consumed = subprocess.CompletedProcess(
            child.args, child.returncode, bytes(first_line), None
        )
        return process_transcript(consumed, note='stdout consumed during readiness')

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    reader.join(10)
    if reader.is_alive():
        failure = _kill_and_describe(child, cwd=None, reason='no stdout line within 10 s')
        reader.join(10)
        raise AssertionError(f'{readiness_transcript()}\n{failure}')
    try:
        try:
            alive = child.poll() is None
            hosts = Path(first_line.decode().strip())
            metadata_exists = (
                hosts / 'sequential_hooks-0.0.0.dist-info' / 'entry_points.txt'
            ).is_file()
        finally:
            result = finish_process(child, timeout=10)
    except AssertionError as error:
        raise AssertionError(f'{readiness_transcript()}\n{error}') from error
    result.stdout = bytes(first_line) + result.stdout
    assert alive, process_transcript(result)
    assert hosts.parent == tmp_path, process_transcript(result)
    assert metadata_exists, process_transcript(result)
    assert result.returncode == 0, process_transcript(result)
    assert not hosts.exists(), process_transcript(result)
