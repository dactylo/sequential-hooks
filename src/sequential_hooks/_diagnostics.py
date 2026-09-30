# pyright: reportPrivateUsage=false

"""Write bounded, synchronized child diagnostics."""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

from sequential_hooks._downstream import is_downstream_closure, silence_descriptor

if TYPE_CHECKING:
    from typing import TextIO

    from sequential_hooks.hosts import Step

_STDERR_CHAIN_LIMIT_BYTES = 4 * 1024 * 1024
_STDERR_STEP_LIMIT_BYTES = 1024 * 1024


class _DiagnosticState:
    """Own diagnostic output synchronization and the shared chain budget."""

    def __init__(self, stream: TextIO) -> None:
        """Initialize shared diagnostic budgets and synchronization."""
        self._chain_bytes = 0
        self._chain_notice_written = False
        self._broken = False
        self._output_draining = False
        self._output_lines: deque[str] = deque()
        self._state_lock = threading.Lock()
        self._stream = stream

    def _drain_output(self, owns_drain: bool) -> None:
        """Drain queued lines when this caller owns diagnostic output."""
        if not owns_drain:
            return
        completed = False
        try:
            while True:
                with self._state_lock:
                    if not self._output_lines:
                        self._output_draining = False
                        completed = True
                        return
                    batch = self._output_lines.popleft()
                if not self._broken:
                    try:
                        self._stream.write(batch)
                        self._stream.flush()
                    except OSError as error:
                        if not is_downstream_closure(error):
                            raise
                        self._broken = True
                        silence_descriptor(self._stream)
        finally:
            if not completed:
                with self._state_lock:
                    self._output_draining = False

    def _enqueue_lines_locked(self, lines: list[str]) -> bool:
        """Queue complete lines and claim an idle drainer."""
        if not lines:
            return False
        self._output_lines.append(''.join(lines))
        if self._output_draining:
            return False
        self._output_draining = True
        return True

    def _enqueue_step_locked(self, step: Step, messages: list[str]) -> bool:
        """Render and queue ordered step lines under the state lock."""
        prefix = f'sequential-hooks: step {step.index} ({step.name}): '
        lines = [prefix + message + '\n' for message in messages]
        return self._enqueue_lines_locked(lines)

    def _write_global(self, message: str) -> None:
        """Queue one global line and drain it when output is idle."""
        with self._state_lock:
            owns_drain = self._enqueue_lines_locked(
                [f'sequential-hooks: {message}\n'],
            )
        self._drain_output(owns_drain)

    def _write_step(self, step: Step, messages: list[str]) -> None:
        """Queue ordered step lines and drain them when output is idle."""
        with self._state_lock:
            owns_drain = self._enqueue_step_locked(step, messages)
        self._drain_output(owns_drain)


class StderrRelay:
    """Forward one child's bounded stderr and retain its native reason.

    Feed raw chunks in arrival order, then call `finish()` after ordinary EOF
    to flush a partial line. Call `close()` during cleanup to stop forwarding
    without diagnostic I/O and retrieve the bounded captured text. Output and
    flush failures other than downstream closure propagate from `feed()` and
    `finish()`; downstream closure latches the shared output closed.
    """

    def __init__(self, state: _DiagnosticState, step: Step) -> None:
        """Bind one child to the shared diagnostic state.

        Args:
            state: Shared diagnostic stream, synchronization, and chain budget.
            step: Child whose stderr is forwarded and labeled.
        """
        self._captured = bytearray()
        self._closed = False
        self._line_buffer = bytearray()
        self._state = state
        self._step = step
        self._step_bytes = 0
        self._step_notice_written = False

    def _decode_line(self, line: bytes | bytearray) -> str:
        """Decode one safe user-facing line."""
        raw_line = bytes(line)
        try:
            return raw_line.decode()
        except UnicodeDecodeError:
            return f'[invalid UTF-8 replaced] {raw_line.decode(errors="replace")}'

    def _complete_line_messages_locked(self, accepted: bytes) -> list[str]:
        """Capture every complete buffered line while holding the state lock."""
        if b'\n' not in accepted:
            return []
        lines = self._line_buffer.split(b'\n')
        self._line_buffer = bytearray(lines.pop())
        return [self._decode_line(line) for line in lines]

    def close(self) -> str:
        """Close without diagnostic I/O and return bounded child stderr text.

        Returns:
            Replacement-decoded child text without diagnostic prefixes.
        """
        with self._state._state_lock:
            self._closed = True
            return self._captured.decode(errors='replace')

    def feed(self, data: bytes) -> None:
        """Forward and retain bytes that fit the step and chain budgets.

        Args:
            data: Next raw child stderr chunk.

        Raises:
            OSError: If writing or flushing diagnostic output fails for a
                reason other than downstream closure. Downstream closure is
                absorbed and latches output closed.
        """
        with self._state._state_lock:
            if self._closed:
                return
            messages: list[str] = []
            step_remaining = max(0, _STDERR_STEP_LIMIT_BYTES - self._step_bytes)
            chain_remaining = max(
                0,
                _STDERR_CHAIN_LIMIT_BYTES - self._state._chain_bytes,
            )
            accepted_count = min(len(data), step_remaining, chain_remaining)
            accepted = data[:accepted_count]
            self._captured.extend(accepted)
            self._line_buffer.extend(accepted)
            self._step_bytes += accepted_count
            self._state._chain_bytes += accepted_count
            messages.extend(self._complete_line_messages_locked(accepted))

            if (
                len(data) > step_remaining
                and step_remaining <= chain_remaining
                and not self._step_notice_written
            ):
                messages.append(
                    f'stderr truncated after {_STDERR_STEP_LIMIT_BYTES} bytes for this step',
                )
                self._step_notice_written = True
            if (
                len(data) > chain_remaining
                and chain_remaining <= step_remaining
                and not self._state._chain_notice_written
            ):
                messages.append(
                    f'stderr truncated after {_STDERR_CHAIN_LIMIT_BYTES} bytes for this chain',
                )
                self._state._chain_notice_written = True
            owns_drain = self._state._enqueue_step_locked(self._step, messages)
        self._state._drain_output(owns_drain)

    def finish(self) -> None:
        """Flush one partial line after ordinary reader EOF.

        Raises:
            OSError: If writing or flushing diagnostic output fails for a
                reason other than downstream closure. Downstream closure is
                absorbed and latches output closed.
        """
        with self._state._state_lock:
            if self._closed:
                return
            messages: list[str] = []
            if self._line_buffer:
                messages.append(self._decode_line(self._line_buffer))
                self._line_buffer.clear()
            owns_drain = self._state._enqueue_step_locked(self._step, messages)
        self._state._drain_output(owns_drain)


class Diagnostics:
    """Write bounded, labeled child diagnostics to one text stream.

    One instance synchronizes writes and shares the aggregate byte budget
    across every relay it creates. Output and flush failures other than
    downstream closure propagate from write operations; downstream closure
    latches output closed.
    """

    def __init__(self, stream: TextIO) -> None:
        """Bind diagnostics to one text output stream.

        Args:
            stream: Text stream that receives user-facing diagnostic lines.
        """
        self._state = _DiagnosticState(stream)

    def relay(self, step: Step) -> StderrRelay:
        """Create a bounded stderr relay for one child.

        Args:
            step: Child whose stderr will be forwarded.

        Returns:
            Relay sharing this diagnostic stream and chain budget.
        """
        return StderrRelay(self._state, step)

    def write_global(self, message: str) -> None:
        """Write a diagnostic that does not belong to one step.

        Args:
            message: Safe user-facing diagnostic text.

        Raises:
            OSError: If writing or flushing diagnostic output fails for a
                reason other than downstream closure. Downstream closure is
                absorbed and latches output closed.
        """
        self._state._write_global(message)

    def write_step(self, step: Step, message: str) -> None:
        """Write one labeled step diagnostic.

        Args:
            step: Child associated with the message.
            message: Safe user-facing diagnostic text.

        Raises:
            OSError: If writing or flushing diagnostic output fails for a
                reason other than downstream closure. Downstream closure is
                absorbed and latches output closed.
        """
        self._state._write_step(step, [message])
