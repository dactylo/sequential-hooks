import contextlib
import io
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, Protocol
from unittest import mock

from sequential_hooks._cli import main
from sequential_hooks._types import StepResult
from tests._distributions import subprocess_environment

# Hang guard, not a performance assertion. Allow for scheduling on shared CI hosts.
CHILD_TIMEOUT_SECONDS = 300
_COLLECT_AFTER_KILL_SECONDS = 10
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')
PROCESS_GROUP_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
_TRANSCRIPT_FIELD_CHARACTERS = 2048
_TRANSCRIPT_HEAD_BYTES = 2048
_TRANSCRIPT_TAIL_BYTES = 6144


class _Outcome(Protocol):
    @property
    def args(self) -> object: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> str | bytes | None: ...

    @property
    def stderr(self) -> str | bytes | None: ...


class _StepOutcome(NamedTuple):
    args: object
    returncode: int | None
    stdout: bytes
    stderr: str


@dataclass(frozen=True, kw_only=True)
class FixtureResult(StepResult):
    command: tuple[str, ...]


def _describe_status(returncode: int | None) -> str:
    if returncode is None:
        return 'not collected'
    if returncode < 0:
        try:
            return f'{returncode} ({signal.Signals(-returncode).name})'
        except ValueError:
            return str(returncode)
    return str(returncode)


def _escape_transcript_line(line: str) -> str:
    line = _CONTROL_CHARACTERS.sub(lambda match: f'\\x{ord(match.group()):02x}', line)
    line = line.replace('##[', '#\\#[')
    body = line.lstrip()
    if body.startswith('::'):
        line = line[: len(line) - len(body)] + '\\' + body
    return line


def _terminate_tree[T: (str, bytes)](process: subprocess.Popen[T]) -> None:
    if sys.platform == 'win32':
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(process.pid)],
                capture_output=True,
                timeout=_COLLECT_AFTER_KILL_SECONDS,
                check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _transcript_field(label: str, value: object) -> str:
    field = repr(value)
    if len(field) > _TRANSCRIPT_FIELD_CHARACTERS:
        omitted = len(field) - _TRANSCRIPT_FIELD_CHARACTERS
        field = f'{field[:_TRANSCRIPT_FIELD_CHARACTERS]} [... {omitted} characters omitted ...]'
    return f'{label}: {_escape_transcript_line(field)}'


def _transcript_stream(label: str, data: str | bytes | None) -> list[str]:
    if data is None:
        return [f'{label}: <not collected>']
    raw = data if isinstance(data, bytes) else data.encode('utf-8', 'backslashreplace')
    if not raw:
        return [f'{label}: <empty>']
    if len(raw) > _TRANSCRIPT_HEAD_BYTES + _TRANSCRIPT_TAIL_BYTES:
        omitted = len(raw) - _TRANSCRIPT_HEAD_BYTES - _TRANSCRIPT_TAIL_BYTES
        field = (
            raw[:_TRANSCRIPT_HEAD_BYTES].decode('utf-8', 'backslashreplace')
            + f'\n[... {omitted} bytes omitted ...]\n'
            + raw[-_TRANSCRIPT_TAIL_BYTES:].decode('utf-8', 'backslashreplace')
        )
    else:
        field = raw.decode('utf-8', 'backslashreplace')
    body = field.replace('\r\n', '\n').removesuffix('\n')
    return [f'{label}:', *(f'  | {_escape_transcript_line(line)}' for line in body.split('\n'))]


def decode_text(data: bytes) -> str:
    return data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


def fixture_result(result: StepResult, command: tuple[str, ...]) -> FixtureResult:
    return FixtureResult(
        result.returncode,
        result.stdout,
        result.blocking_stderr,
        result.failure,
        result.containment,
        command=command,
    )


def process_transcript(
    result: _Outcome,
    *,
    cwd: Path | str | None = None,
    note: str | None = None,
) -> str:
    lines = [_transcript_field('command', result.args)]
    if cwd is not None:
        lines.append(_transcript_field('cwd', str(cwd)))
    if note is not None:
        lines.append(f'note: {_escape_transcript_line(note)}')
    lines.append(f'status: {_describe_status(result.returncode)}')
    lines.extend(_transcript_stream('stdout', result.stdout))
    lines.extend(_transcript_stream('stderr', result.stderr))
    return '\n'.join(lines)


def _kill_and_describe[T: (str, bytes)](
    process: subprocess.Popen[T],
    *,
    cwd: Path | str | None,
    reason: str,
) -> str:
    _terminate_tree(process)
    try:
        stdout, stderr = process.communicate(timeout=_COLLECT_AFTER_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        stdout = stderr = None
        reason = f'{reason}; a descendant outside the process group still holds the output pipes'
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_COLLECT_AFTER_KILL_SECONDS)
    killed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    return process_transcript(killed, cwd=cwd, note=reason)


def step_transcript(
    result: StepResult, diagnostics: str = '', *, command: object | None = None
) -> str:
    displayed_command = getattr(result, 'command', '<hook step>') if command is None else command
    outcome = _StepOutcome(
        displayed_command, result.returncode, result.stdout, result.blocking_stderr
    )
    return '\n'.join(
        [process_transcript(outcome), *_transcript_stream('diagnostics', diagnostics)]
    )


def finish_process[T: (str, bytes)](
    process: subprocess.Popen[T],
    *,
    stdin: T | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[T]:
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            _kill_and_describe(process, cwd=cwd, reason=f'timed out after {timeout:g} s')
        ) from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def invoke_cli(
    argv: Sequence[str],
    stdin: bytes | io.BytesIO = b'',
    *,
    cwd: Path | str | None = None,
) -> tuple[int, bytes, str]:
    stdin_buffer = stdin if isinstance(stdin, io.BytesIO) else io.BytesIO(stdin)
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_buffer, encoding='utf-8', newline='')
    stderr = io.TextIOWrapper(stderr_buffer, encoding='utf-8', newline='')
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(sys, 'stdin', io.TextIOWrapper(stdin_buffer, encoding='utf-8'))
        )
        stack.enter_context(contextlib.redirect_stdout(stdout))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        if cwd is not None:
            stack.enter_context(contextlib.chdir(cwd))
        status = main(argv)
        stdout.flush()
        stderr.flush()
    return status, stdout_buffer.getvalue(), stderr_buffer.getvalue().decode('utf-8')


def run_cli(  # noqa: PLR0913
    argv: Sequence[str],
    *,
    expect: int | None = None,
    stdin: str = '',
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [sys.executable, '-m', 'sequential_hooks', *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        env=dict(subprocess_environment() if env is None else env),
        cwd=cwd,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    result = finish_process(process, stdin=stdin, timeout=timeout, cwd=cwd)
    if expect is not None and result.returncode != expect:
        raise AssertionError(f'expected status {expect}\n{process_transcript(result, cwd=cwd)}')
    return result


def run_closing_after_first_line(
    argv: Sequence[str],
    *,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        [sys.executable, '-m', 'sequential_hooks', *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=subprocess_environment(),
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    stdout = process.stdout
    assert stdout is not None
    descriptor = stdout.fileno()

    def read_first_line() -> None:
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk or b'\n' in chunk:
                return

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            _kill_and_describe(process, cwd=None, reason=f'no stdout line within {timeout:g} s')
        )
    stdout.close()
    process.stdout = None  # communicate() must not read the closed pipe on Windows
    return finish_process(process, timeout=timeout)


def run_setup(  # noqa: PLR0913
    command: Sequence[str | bytes],
    *,
    check: bool = True,
    cwd: Path | str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        cwd=cwd,
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    display_cwd = os.fsdecode(cwd) if isinstance(cwd, bytes) else cwd
    result = finish_process(process, stdin=stdin, timeout=timeout, cwd=display_cwd)
    if check and result.returncode != 0:
        raise AssertionError(
            f'setup command failed\n{process_transcript(result, cwd=display_cwd)}'
        )
    return result


def run_with_closed_output(
    argv: Sequence[str],
    *,
    closed: Literal['stdout', 'stderr'] = 'stdout',
    stdin: bytes | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # every write to the closed stream happens after its reader closed
    try:
        process = subprocess.Popen(
            [sys.executable, '-m', 'sequential_hooks', *argv],
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            stdout=write_fd if closed == 'stdout' else subprocess.PIPE,
            stderr=write_fd if closed == 'stderr' else subprocess.PIPE,
            env=subprocess_environment(),
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
    finally:
        os.close(write_fd)
    return finish_process(process, stdin=stdin, timeout=timeout)
