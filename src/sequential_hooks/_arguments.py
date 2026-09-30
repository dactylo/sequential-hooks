# pyright: reportPrivateUsage=false

"""Parse validated wrapper arguments into hook invocations."""

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sequential_hooks._types import Step

_MAX_STEP_COUNT = 3


@dataclass(frozen=True)
class HookInvocation:
    """Describe one validated hook-mode invocation.

    Agent and event names are literal strings; the selected host validates them
    after it loads.

    Args:
        host: Selected coding-agent name.
        event: Explicit `--event` value, otherwise `None`.
        step_timeout: Positive finite per-step timeout in seconds, if set.
        allow_on_failure: Whether approved pre-tool failures may continue.
        steps: One to three literal child commands in execution order.

    Attributes:
        host: Selected coding-agent name.
        event: Explicit `--event` value, otherwise `None`.
        step_timeout: Positive finite per-step timeout in seconds, if set.
        allow_on_failure: Whether approved pre-tool failures may continue.
        steps: One to three literal child commands in execution order.
    """

    host: str
    event: str | None
    step_timeout: float | None
    allow_on_failure: bool
    steps: tuple[Step, ...]


class InvocationError(ValueError):
    """Report invalid syntax with safely recognized agent context.

    Attributes:
        host: Coding-agent name given before the error, if any.
        event: Event name given before the error, if any.
    """

    host: str | None
    event: str | None

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        event: str | None = None,
    ) -> None:
        """Retain invalid syntax details and recognized host context.

        Args:
            message: Safe diagnostic text.
            host: Coding-agent name given before the error, if any.
            event: Event name given before the error, if any.
        """
        super().__init__(message)
        self.host = host
        self.event = event


@dataclass
class _ParseState:
    """Retain safely recognized wrapper option state."""

    _host: str | None = None
    _event: str | None = None
    _step_timeout: float | None = None
    _allow_on_failure: bool = False


def _error(message: str, host: str | None, event: str | None) -> InvocationError:
    """Build an error carrying only safely recognized context."""
    return InvocationError(message, host=host, event=event)


def _consume_allow_on_failure(state: _ParseState, index: int) -> int:
    """Consume one non-repeating failure override."""
    if state._allow_on_failure:
        raise _error(
            '--allow-on-failure may be provided only once',
            state._host,
            state._event,
        )
    state._allow_on_failure = True
    return index + 1


def _consume_timeout(tokens: tuple[str, ...], state: _ParseState, index: int) -> int:
    """Consume one positive finite step timeout."""
    if state._step_timeout is not None:
        raise _error('--step-timeout may be provided only once', state._host, state._event)
    if index + 1 >= len(tokens):
        raise _error('--step-timeout requires a value', state._host, state._event)
    value = tokens[index + 1]
    try:
        timeout = float(value)
    except ValueError as error:
        raise _error(
            '--step-timeout must be a positive finite number',
            state._host,
            state._event,
        ) from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise _error(
            '--step-timeout must be a positive finite number',
            state._host,
            state._event,
        )
    state._step_timeout = timeout
    return index + 2


def _is_option_like(value: str) -> bool:
    """Return whether a token cannot be a host or event name."""
    return not value or value.startswith('-')


def _consume_agent(tokens: tuple[str, ...], state: _ParseState, index: int) -> int:
    """Consume one literal coding-agent name."""
    if state._host is not None:
        raise _error('--agent may be provided only once', state._host, state._event)
    if index + 1 >= len(tokens) or _is_option_like(tokens[index + 1]):
        raise _error('--agent requires a value', None, state._event)
    state._host = tokens[index + 1]
    return index + 2


def _consume_event(tokens: tuple[str, ...], state: _ParseState, index: int) -> int:
    """Consume one literal event name."""
    if state._event is not None:
        raise _error('--event may be provided only once', state._host, state._event)
    if index + 1 >= len(tokens) or _is_option_like(tokens[index + 1]):
        raise _error('--event requires a value', state._host, None)
    state._event = tokens[index + 1]
    return index + 2


def _parse_steps(
    child_arguments: tuple[str, ...],
    host: str,
    event: str | None,
) -> tuple[Step, ...]:
    """Split exact separators into non-empty steps."""
    if not child_arguments:
        raise _error('at least one child command is required', host, event)
    vectors: list[list[str]] = [[]]
    for argument in child_arguments:
        if argument == '--':
            if not vectors[-1]:
                raise _error('child separator creates an empty step', host, event)
            if len(vectors) == _MAX_STEP_COUNT:
                raise _error('at most three child commands are accepted', host, event)
            vectors.append([])
        else:
            vectors[-1].append(argument)
    if not vectors[-1]:
        raise _error('child separator creates an empty step', host, event)
    return tuple(Step(step_index, tuple(vector)) for step_index, vector in enumerate(vectors, 1))


def _parse_wrapper_options(tokens: tuple[str, ...]) -> tuple[_ParseState, int]:
    """Consume wrapper options until the first child token."""
    state = _ParseState()
    consumers: dict[str, Callable[[int], int]] = {
        '-a': lambda index: _consume_agent(tokens, state, index),
        '-e': lambda index: _consume_event(tokens, state, index),
        '--allow-on-failure': lambda index: _consume_allow_on_failure(state, index),
        '--event': lambda index: _consume_event(tokens, state, index),
        '--agent': lambda index: _consume_agent(tokens, state, index),
        '--step-timeout': lambda index: _consume_timeout(tokens, state, index),
    }
    index = 0
    while index < len(tokens) and tokens[index].startswith('-'):
        argument = tokens[index]
        try:
            consumer = consumers[argument]
        except KeyError as error:
            raise _error(
                'unknown wrapper option; use --help for supported syntax',
                state._host,
                state._event,
            ) from error
        index = consumer(index)
    return state, index


def parse_hook_argv(argv: Iterable[str]) -> HookInvocation:
    """Parse one hook-mode command without reading stdin or loading a host.

    Args:
        argv: Tokens after the executable name.

    Returns:
        Coding-agent name, event option, timeout, failure option, and steps.

    Raises:
        InvocationError: If wrapper syntax is invalid or `--agent` is missing.
    """
    tokens = tuple(argv)
    state, child_index = _parse_wrapper_options(tokens)
    if state._host is None:
        raise _error('--agent is required', None, None)
    steps = _parse_steps(tokens[child_index:], state._host, state._event)
    return HookInvocation(
        state._host,
        state._event,
        state._step_timeout,
        state._allow_on_failure,
        steps,
    )
