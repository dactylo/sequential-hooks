import errno
import threading
from collections.abc import Buffer
from io import StringIO
from typing import SupportsIndex, TextIO, cast

import pytest

from sequential_hooks import _diagnostics, _downstream
from sequential_hooks._diagnostics import (
    _STDERR_CHAIN_LIMIT_BYTES,
    _STDERR_STEP_LIMIT_BYTES,
    Diagnostics,
)
from sequential_hooks._types import Step


class _BlockingTextStream:
    def __init__(self) -> None:
        self.release_write = threading.Event()
        self.write_started = threading.Event()
        self._lock = threading.Lock()
        self._writes: list[str] = []

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        with self._lock:
            return ''.join(self._writes)

    def write(self, value: str) -> int:
        if not self.write_started.is_set():
            self.write_started.set()
            self.release_write.wait()
        with self._lock:
            self._writes.append(value)
        return len(value)


class _BrokenPipeTextIO(StringIO):
    def write(self, _value: str) -> int:
        raise BrokenPipeError


class _CountingTextStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0
        self.write_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()

    def write(self, value: str) -> int:
        self.write_count += 1
        return super().write(value)


class _FailingTextStream:
    def __init__(self) -> None:
        self.error = OSError('diagnostic write failed')

    def flush(self) -> None:
        raise AssertionError('flush was not expected after a failed write')

    def write(self, _value: str) -> int:
        raise self.error


class _SplitCountingBuffer(bytearray):
    splits = 0

    def split(
        self,
        sep: Buffer | None = None,
        maxsplit: SupportsIndex = -1,
    ) -> list[bytearray]:
        _SplitCountingBuffer.splits += 1
        return super().split(sep, maxsplit)


def test_diagnostics_absorb_broken_pipe() -> None:
    diagnostics = Diagnostics(_BrokenPipeTextIO())

    diagnostics.write_global('first')
    diagnostics.write_global('second')


def test_diagnostics_active_drainer_propagates_output_error() -> None:
    stream = _FailingTextStream()
    diagnostics = Diagnostics(cast('TextIO', stream))

    with pytest.raises(OSError, match='diagnostic write failed') as caught:
        diagnostics.write_global('failed')

    assert caught.value is stream.error


def test_diagnostics_enqueue_while_output_drainer_is_blocked() -> None:
    stream = _BlockingTextStream()
    diagnostics = Diagnostics(cast('TextIO', stream))
    second_finished = threading.Event()
    third_finished = threading.Event()

    def write_first() -> None:
        diagnostics.write_global('first')

    def write_second() -> None:
        try:
            diagnostics.write_step(Step(2, ('second-hook',)), 'second')
        finally:
            second_finished.set()

    def write_third() -> None:
        try:
            diagnostics.write_global('third')
        finally:
            third_finished.set()

    first = threading.Thread(target=write_first, daemon=True)
    second = threading.Thread(target=write_second, daemon=True)
    third = threading.Thread(target=write_third, daemon=True)
    first.start()
    try:
        assert stream.write_started.wait(0.5)
        second.start()
        second_returned_while_blocked = second_finished.wait(0.5)
        third.start()
        third_returned_while_blocked = third_finished.wait(0.5)
    finally:
        stream.release_write.set()
        first.join(0.5)
        second.join(0.5)
        third.join(0.5)

    assert second_returned_while_blocked
    assert third_returned_while_blocked
    assert not first.is_alive()
    assert not second.is_alive()
    assert not third.is_alive()
    assert stream.getvalue() == (
        'sequential-hooks: first\n'
        'sequential-hooks: step 2 (second-hook): second\n'
        'sequential-hooks: third\n'
    )


@pytest.mark.parametrize('error', [BrokenPipeError(), OSError(errno.EINVAL, 'closed')])
def test_diagnostics_latch_closure_and_silence_once(
    error: OSError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ClosedTextStream(StringIO):
        writes = 0

        def write(self, _value: str) -> int:
            self.writes += 1
            raise error

    silenced: list[object] = []
    stream = _ClosedTextStream()
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    monkeypatch.setattr(_diagnostics, 'silence_descriptor', silenced.append)
    diagnostics = Diagnostics(stream)

    diagnostics.write_global('first')
    diagnostics.write_global('second')

    assert stream.writes == 1
    assert silenced == [stream]


def test_diagnostics_propagate_unrelated_windows_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError(errno.EBADF, 'bad')

    class _BadTextStream(StringIO):
        def write(self, _value: str) -> int:
            raise error

    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    diagnostics = Diagnostics(_BadTextStream())

    with pytest.raises(OSError, match='bad') as caught:
        diagnostics.write_global('failed')
    assert caught.value is error


def test_diagnostics_use_exact_global_and_step_prefixes() -> None:
    stream = StringIO()
    diagnostics = Diagnostics(stream)

    diagnostics.write_global('invalid input')
    diagnostics.write_step(Step(2, ('/usr/bin/second-hook', 'hook')), 'failed')

    assert stream.getvalue() == (
        'sequential-hooks: invalid input\nsequential-hooks: step 2 (second-hook): failed\n'
    )


def test_relay_batches_complete_lines_from_one_feed() -> None:
    stream = _CountingTextStream()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))

    relay.feed(b'first\nsecond\n')
    relay.close()

    assert stream.getvalue() == (
        'sequential-hooks: step 1 (hook): first\nsequential-hooks: step 1 (hook): second\n'
    )
    assert stream.write_count == 1
    assert stream.flush_count == 1


def test_relay_close_does_not_wait_for_blocked_diagnostic_output() -> None:
    stream = _BlockingTextStream()
    relay = Diagnostics(cast('TextIO', stream)).relay(Step(1, ('hook',)))
    feed_finished = threading.Event()
    close_finished = threading.Event()
    captured: list[str] = []

    def close_relay() -> None:
        captured.append(relay.close())
        close_finished.set()

    def feed_relay() -> None:
        relay.feed(b'first\nsecond\n')
        feed_finished.set()

    feeder = threading.Thread(target=feed_relay, daemon=True)
    closer = threading.Thread(target=close_relay, daemon=True)
    feeder.start()
    assert stream.write_started.wait(0.5)
    closer.start()
    completed_within_bound = close_finished.wait(0.5)
    stream.release_write.set()
    feeder.join(0.5)
    closer.join(0.5)

    assert completed_within_bound
    assert not feeder.is_alive()
    assert not closer.is_alive()
    assert feed_finished.is_set()
    assert captured == ['first\nsecond\n']
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (hook): first\nsequential-hooks: step 1 (hook): second\n'
    )


def test_relay_counts_bytes_before_replacement_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_diagnostics, '_STDERR_STEP_LIMIT_BYTES', 2)
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))

    relay.feed('\u00e9x'.encode())
    relay.finish()

    assert relay.close() == '\u00e9'
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (hook): stderr truncated after 2 bytes for this step\n'
        'sequential-hooks: step 1 (hook): \u00e9\n'
    )


def test_relay_does_not_rescan_newline_free_tail() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))
    relay.feed(b'seed')
    relay_state = vars(relay)
    relay_state['_line_buffer'] = _SplitCountingBuffer(relay_state['_line_buffer'])
    _SplitCountingBuffer.splits = 0

    for _ in range(1000):
        relay.feed(b'x')

    assert _SplitCountingBuffer.splits == 0


def test_relay_emits_line_split_across_single_byte_feeds() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))

    for byte in b'alpha\n':
        relay.feed(bytes([byte]))

    assert 'alpha' in stream.getvalue()
    assert stream.getvalue().count('\n') == 1


def test_relay_enforces_exact_chain_byte_limit_once() -> None:
    assert _STDERR_CHAIN_LIMIT_BYTES == 4 * 1024 * 1024
    stream = StringIO()
    diagnostics = Diagnostics(stream)
    bounded = b'x' * _STDERR_STEP_LIMIT_BYTES

    for index in range(1, 5):
        relay = diagnostics.relay(Step(index, ('hook',)))
        relay.feed(bounded)
        relay.finish()
        assert relay.close() == bounded.decode()

    exhausted = diagnostics.relay(Step(5, ('hook',)))
    exhausted.feed(b'excess\n')
    exhausted.feed(b'more excess\n')

    assert exhausted.close() == ''
    assert (
        stream.getvalue().count(
            'sequential-hooks: step 5 (hook): '
            'stderr truncated after 4194304 bytes for this chain\n'
        )
        == 1
    )
    assert 'excess' not in stream.getvalue()


def test_relay_enforces_exact_per_step_byte_limit_once() -> None:
    assert _STDERR_STEP_LIMIT_BYTES == 1024 * 1024
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))
    bounded = b'x' * _STDERR_STEP_LIMIT_BYTES

    relay.feed(bounded)
    relay.feed(b'excess\n')
    relay.feed(b'more excess\n')
    relay.finish()

    assert relay.close() == bounded.decode()
    assert (
        stream.getvalue().count(
            'sequential-hooks: step 1 (hook): stderr truncated after 1048576 bytes for this step\n'
        )
        == 1
    )
    assert 'excess' not in stream.getvalue()


def test_relay_finish_flushes_partial_line_exactly_once() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))
    relay.feed(b'partial')

    relay.finish()
    relay.finish()
    first_close = relay.close()
    second_close = relay.close()

    assert first_close == second_close == 'partial'
    assert stream.getvalue() == 'sequential-hooks: step 1 (hook): partial\n'


def test_relay_ignores_feed_and_finish_after_close() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))
    relay.feed(b'before\n')

    captured = relay.close()
    relay.feed(b'after\n')
    relay.finish()

    assert relay.close() == captured == 'before\n'
    assert stream.getvalue() == 'sequential-hooks: step 1 (hook): before\n'


def test_relay_labels_complete_and_partial_lines() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(2, ('/usr/bin/second-hook', 'hook')))

    relay.feed(b'first\nsecond')
    relay.finish()

    assert relay.close() == 'first\nsecond'
    assert stream.getvalue() == (
        'sequential-hooks: step 2 (second-hook): first\n'
        'sequential-hooks: step 2 (second-hook): second\n'
    )


def test_relay_marks_invalid_utf8_but_returns_unprefixed_blocking_text() -> None:
    stream = StringIO()
    relay = Diagnostics(stream).relay(Step(1, ('hook',)))

    relay.feed(b'bad:\xff\n')

    assert relay.close() == 'bad:\ufffd\n'
    assert stream.getvalue() == (
        'sequential-hooks: step 1 (hook): [invalid UTF-8 replaced] bad:\ufffd\n'
    )
