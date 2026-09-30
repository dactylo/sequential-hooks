"""Run ordered child steps through one host adapter."""

from collections.abc import Iterable

from sequential_hooks import _executor
from sequential_hooks._types import Step
from sequential_hooks.hosts import FinalResponse, HostAdapter


def run_pipeline(
    adapter: HostAdapter,
    steps: Iterable[Step],
    executor: _executor.ProcessExecutor,
    step_timeout: float | None,
    *,
    allow_on_failure: bool,
) -> FinalResponse:
    """Run ordered steps until the adapter stops or finalizes.

    Args:
        adapter: Host-neutral interface to native protocol state.
        steps: Child commands in execution order.
        executor: Bounded process executor for each child.
        step_timeout: Independent timeout in seconds for each child, or `None`
            for no step execution deadline.
        allow_on_failure: Whether explicit uncontained fallback is allowed.

    Returns:
        Terminal or finalized native host response.

    Raises:
        _executor.ForwardedSignal: If the executor forwards a handled wrapper
            signal, propagated unchanged.
        Exception: Any executor or adapter invariant exception, propagated
            unchanged.
    """
    if not adapter.should_run_steps:
        return adapter.finalize()
    for step in steps:
        result = executor.run(
            step,
            adapter.step_input(),
            step_timeout,
            allow_uncontained=allow_on_failure,
        )
        if adapter.consume(step, result) == 'stop':
            break
    return adapter.finalize()
