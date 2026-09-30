import threading
from dataclasses import dataclass, field

import pytest

from sequential_hooks._executor import ForwardedSignal, ProcessExecutor
from sequential_hooks._runner import run_pipeline
from sequential_hooks._types import Step, StepResult
from sequential_hooks.hosts import ChainControl, FinalResponse, NativeResponse
from sequential_hooks.hosts import Step as StepView
from sequential_hooks.hosts import StepResult as StepResultView

_EVENT_WAIT_SECONDS = 1.0
_NON_ENTRY_WAIT_SECONDS = 0.05


@dataclass
class _GatedExecutor(ProcessExecutor):
    entered: tuple[threading.Event, ...]
    releases: tuple[threading.Event, ...]
    calls: list[Step] = field(default_factory=list[Step])

    def run(
        self,
        step: Step,
        input_bytes: bytes,
        timeout: float | None,
        *,
        allow_uncontained: bool,
    ) -> StepResult:
        del input_bytes, timeout, allow_uncontained
        position = step.index - 1
        self.calls.append(step)
        self.entered[position].set()
        if not self.releases[position].wait(_EVENT_WAIT_SECONDS):
            raise AssertionError(f'step {step.index} was not released')
        return StepResult(step.index, f'result-{step.index}'.encode(), '')


@dataclass
class _RecordingExecutor(ProcessExecutor):
    calls: list[tuple[Step, bytes, float | None, bool]] = field(
        default_factory=list[tuple[Step, bytes, float | None, bool]]
    )
    error: BaseException | None = None
    error_at: int | None = None
    results: list[StepResult] = field(default_factory=list[StepResult])

    def run(
        self,
        step: Step,
        input_bytes: bytes,
        timeout: float | None,
        *,
        allow_uncontained: bool,
    ) -> StepResult:
        self.calls.append((step, input_bytes, timeout, allow_uncontained))
        if step.index == self.error_at and self.error is not None:
            raise self.error
        result = StepResult(step.index, f'result-{step.index}'.encode(), '')
        self.results.append(result)
        return result


class _SentinelError(RuntimeError):
    pass


@dataclass
class _FakeAdapter:
    consume_error: _SentinelError | None = None
    consume_error_at: int | None = None
    consumed: list[tuple[StepView, StepResultView]] = field(
        default_factory=list[tuple[StepView, StepResultView]]
    )
    events: list[str] = field(default_factory=list[str])
    finalize_error: _SentinelError | None = None
    finalize_calls: int = 0
    payload_counter: int = 0
    response: NativeResponse = field(default_factory=lambda: NativeResponse(b'final\n'))
    step_input_error: _SentinelError | None = None
    step_input_error_at: int | None = None
    stop_at: int | None = None
    run_steps: bool = True

    def consume(self, step: StepView, result: StepResultView) -> ChainControl:
        self.consumed.append((step, result))
        self.events.append(f'consume {step.index}')
        if step.index == self.consume_error_at and self.consume_error is not None:
            raise self.consume_error
        self.payload_counter += 1
        if step.index == self.stop_at:
            return 'stop'
        return 'continue'

    @property
    def event(self) -> str:
        return 'PreToolUse'

    def finalize(self) -> NativeResponse:
        self.events.append('finalize')
        self.finalize_calls += 1
        if self.finalize_error is not None:
            raise self.finalize_error
        return self.response

    @property
    def should_run_steps(self) -> bool:
        return self.run_steps

    def step_input(self) -> bytes:
        if self.payload_counter == self.step_input_error_at and self.step_input_error is not None:
            raise self.step_input_error
        return f'opaque-payload-{self.payload_counter}'.encode()


def _make_steps(count: int) -> tuple[Step, ...]:
    return tuple(Step(index, (f'step-{index}',)) for index in range(1, count + 1))


def test_run_pipeline_finalizes_once_after_last_continued_step() -> None:
    response = NativeResponse(b'complete\n')
    adapter = _FakeAdapter(response=response)
    executor = _RecordingExecutor()

    actual = run_pipeline(
        adapter,
        _make_steps(2),
        executor,
        None,
        allow_on_failure=False,
    )

    assert actual is response
    assert adapter.events == ['consume 1', 'consume 2', 'finalize']
    assert adapter.finalize_calls == 1


def test_run_pipeline_forwards_adapter_consume_exception_unchanged() -> None:
    error = _SentinelError('consume failed')
    adapter = _FakeAdapter(consume_error=error, consume_error_at=2)
    executor = _RecordingExecutor()

    with pytest.raises(_SentinelError) as raised:
        run_pipeline(
            adapter,
            _make_steps(3),
            executor,
            None,
            allow_on_failure=False,
        )

    assert raised.value is error
    assert [call[0].index for call in executor.calls] == [1, 2]
    assert [step.index for step, _result in adapter.consumed] == [1, 2]
    assert adapter.finalize_calls == 0


def test_run_pipeline_forwards_adapter_finalize_exception_unchanged() -> None:
    error = _SentinelError('finalize failed')
    adapter = _FakeAdapter(finalize_error=error)
    executor = _RecordingExecutor()

    with pytest.raises(_SentinelError) as raised:
        run_pipeline(
            adapter,
            _make_steps(2),
            executor,
            None,
            allow_on_failure=False,
        )

    assert raised.value is error
    assert [call[0].index for call in executor.calls] == [1, 2]
    assert [step.index for step, _result in adapter.consumed] == [1, 2]
    assert adapter.finalize_calls == 1


def test_run_pipeline_forwards_adapter_step_input_exception_unchanged() -> None:
    error = _SentinelError('step input failed')
    adapter = _FakeAdapter(step_input_error=error, step_input_error_at=1)
    executor = _RecordingExecutor()

    with pytest.raises(_SentinelError) as raised:
        run_pipeline(
            adapter,
            _make_steps(3),
            executor,
            None,
            allow_on_failure=False,
        )

    assert raised.value is error
    assert [call[0].index for call in executor.calls] == [1]
    assert [step.index for step, _result in adapter.consumed] == [1]
    assert adapter.finalize_calls == 0


@pytest.mark.parametrize('allow_on_failure', [False, True])
def test_run_pipeline_forwards_allow_on_failure_unchanged(allow_on_failure: bool) -> None:
    adapter = _FakeAdapter()
    executor = _RecordingExecutor()

    run_pipeline(
        adapter,
        _make_steps(3),
        executor,
        None,
        allow_on_failure=allow_on_failure,
    )

    assert [call[3] for call in executor.calls] == [allow_on_failure] * 3


def test_run_pipeline_forwards_executor_exception_unchanged() -> None:
    error = _SentinelError('executor failed')
    adapter = _FakeAdapter()
    executor = _RecordingExecutor(error=error, error_at=2)

    with pytest.raises(_SentinelError) as raised:
        run_pipeline(
            adapter,
            _make_steps(3),
            executor,
            None,
            allow_on_failure=False,
        )

    assert raised.value is error
    assert [call[0].index for call in executor.calls] == [1, 2]
    assert [step.index for step, _result in adapter.consumed] == [1]
    assert adapter.finalize_calls == 0


def test_run_pipeline_forwards_executor_signal_unchanged() -> None:
    forwarded = ForwardedSignal(15)
    adapter = _FakeAdapter()
    executor = _RecordingExecutor(error=forwarded, error_at=2)

    with pytest.raises(ForwardedSignal) as raised:
        run_pipeline(
            adapter,
            _make_steps(3),
            executor,
            None,
            allow_on_failure=False,
        )

    assert raised.value is forwarded
    assert [call[0].index for call in executor.calls] == [1, 2]
    assert [step.index for step, _result in adapter.consumed] == [1]
    assert adapter.finalize_calls == 0


def test_run_pipeline_forwards_timeout_independently_to_every_step() -> None:
    adapter = _FakeAdapter()
    executor = _RecordingExecutor()

    run_pipeline(adapter, _make_steps(3), executor, 2.75, allow_on_failure=False)

    assert [call[2] for call in executor.calls] == [2.75, 2.75, 2.75]


def test_run_pipeline_inapplicable_invocation_finalizes_without_steps() -> None:
    adapter = _FakeAdapter(
        run_steps=False,
        step_input_error=_SentinelError('step input was read'),
        step_input_error_at=0,
    )
    executor = _RecordingExecutor()

    actual = run_pipeline(
        adapter,
        _make_steps(2),
        executor,
        None,
        allow_on_failure=False,
    )

    assert actual is adapter.response
    assert executor.calls == []
    assert adapter.consumed == []
    assert adapter.finalize_calls == 1
    assert adapter.events == ['finalize']


def test_run_pipeline_pairs_each_step_with_exact_executor_result() -> None:
    adapter = _FakeAdapter()
    executor = _RecordingExecutor()
    steps = _make_steps(3)

    run_pipeline(adapter, steps, executor, None, allow_on_failure=False)

    assert len({id(result) for result in executor.results}) == len(steps)
    assert len(adapter.consumed) == len(executor.results)
    for (consumed_step, consumed_result), call, executor_result in zip(
        adapter.consumed,
        executor.calls,
        executor.results,
        strict=True,
    ):
        assert consumed_step is call[0]
        assert consumed_result is executor_result


def test_run_pipeline_passes_updated_opaque_adapter_input() -> None:
    adapter = _FakeAdapter()
    executor = _RecordingExecutor()

    run_pipeline(adapter, _make_steps(3), executor, None, allow_on_failure=False)

    assert [call[1] for call in executor.calls] == [
        b'opaque-payload-0',
        b'opaque-payload-1',
        b'opaque-payload-2',
    ]


@pytest.mark.parametrize('step_count', [1, 2, 3])
def test_run_pipeline_runs_steps_in_exact_order(step_count: int) -> None:
    adapter = _FakeAdapter()
    executor = _RecordingExecutor()
    steps = _make_steps(step_count)

    run_pipeline(adapter, steps, executor, None, allow_on_failure=False)

    assert [call[0] for call in executor.calls] == list(steps)


def test_run_pipeline_stops_before_later_steps_and_finalizes_once() -> None:
    response = NativeResponse(b'stopped\n', 7)
    adapter = _FakeAdapter(response=response, stop_at=2)
    executor = _RecordingExecutor()

    actual = run_pipeline(
        adapter,
        _make_steps(3),
        executor,
        4.5,
        allow_on_failure=True,
    )

    assert actual is response
    assert [call[0].index for call in executor.calls] == [1, 2]
    assert adapter.events == ['consume 1', 'consume 2', 'finalize']
    assert adapter.finalize_calls == 1


def test_run_pipeline_waits_for_each_executor_call_before_starting_next() -> None:
    adapter = _FakeAdapter()
    entered = tuple(threading.Event() for _index in range(3))
    releases = tuple(threading.Event() for _index in range(3))
    executor = _GatedExecutor(entered, releases)
    responses: list[FinalResponse] = []
    steps = _make_steps(3)

    runner = threading.Thread(
        daemon=True,
        target=lambda: responses.append(
            run_pipeline(
                adapter,
                steps,
                executor,
                None,
                allow_on_failure=False,
            )
        ),
    )
    runner.start()
    try:
        assert entered[0].wait(_EVENT_WAIT_SECONDS)
        assert not entered[1].wait(_NON_ENTRY_WAIT_SECONDS)
        assert adapter.finalize_calls == 0

        releases[0].set()
        assert entered[1].wait(_EVENT_WAIT_SECONDS)
        assert not entered[2].wait(_NON_ENTRY_WAIT_SECONDS)
        assert adapter.finalize_calls == 0

        releases[1].set()
        assert entered[2].wait(_EVENT_WAIT_SECONDS)
        assert adapter.finalize_calls == 0

        releases[2].set()
    finally:
        for release in releases:
            release.set()
        runner.join(_EVENT_WAIT_SECONDS)

    assert not runner.is_alive()
    assert responses == [adapter.response]
    assert executor.calls == list(steps)
    assert adapter.finalize_calls == 1
