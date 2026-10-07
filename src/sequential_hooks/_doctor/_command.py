# pyright: reportPrivateUsage=false

"""Inspect registered hook commands without executing them."""

import math
import re
import shlex
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PureWindowsPath

from sequential_hooks._arguments import InvocationError, parse_hook_argv
from sequential_hooks._registry import host_requirements
from sequential_hooks.hosts import ContractError, InspectedCommand, LaunchSemantics, Registration

_ENVIRONMENT_PREFIX = re.compile(r'^\s*[A-Za-z_][A-Za-z0-9_]*=')
_NEGATION_PREFIX = re.compile(r'^\s*!(?:\s|$)')
_SECRET_SEGMENTS = frozenset({'apikey', 'credential', 'password', 'secret', 'token'})
_SUMMARY_LIMIT = 240
_WRAPPER_NAMES = frozenset({'sequential-hooks', 'sequential-hooks.exe'})


@dataclass
class _CommandLexState:
    """Retain lexical state while reading one raw command token."""

    _equals: int | None
    _escaped: bool
    _logical: list[str]
    _outer_quote_end: int | None
    _quote: str | None
    _value_start: int | None


@dataclass(frozen=True)
class _CommandToken:
    """Describe one raw command token and its lexical content."""

    _start: int
    _end: int
    _content_end: int
    _text: str
    _value_start: int | None


@dataclass
class _ContractState:
    """Retain a safely parsed leading wrapper-option prefix."""

    _allow_seen: bool = False
    _event_seen: bool = False
    _event: str | None = None
    _host_seen: bool = False
    _host: str | None = None
    _timeout_seen: bool = False


def _append_token_character(state: _CommandLexState, character: str, index: int) -> None:
    """Append one logical token character and retain its raw equals span."""
    state._logical.append(character)
    if character == '=' and state._equals is None:
        state._equals = index
        state._value_start = index + 1


def _consume_contract_option(
    state: _ContractState,
    option: str,
    value: str,
) -> bool:
    """Consume one value-bearing option when it is well formed."""
    if not value:
        return False
    valid = True
    if option in {'-e', '--event'}:
        if state._event_seen:
            valid = False
        else:
            state._event_seen = True
            state._event = value
    elif option in {'-a', '--agent'}:
        if state._host_seen:
            valid = False
        else:
            state._host_seen = True
            state._host = value
    elif state._timeout_seen:
        valid = False
    else:
        state._timeout_seen = True
        try:
            timeout = float(value)
        except ValueError:
            valid = False
        else:
            valid = math.isfinite(timeout) and timeout > 0
    return valid


def _consume_token_character(
    state: _CommandLexState,
    character: str,
    index: int,
    *,
    leading_quote: str | None,
) -> bool:
    """Consume one source character or identify an unquoted token boundary."""
    if state._escaped:
        if character != '\n':
            _append_token_character(state, character, index)
        state._escaped = False
    elif character == '\\' and state._quote != "'":
        state._escaped = True
    elif character in {"'", '"'} and state._quote in {None, character}:
        if state._quote is None:
            state._quote = character
        else:
            if state._value_start == index:
                state._value_start = index + 1
            if leading_quote == character and state._outer_quote_end is None:
                state._outer_quote_end = index
            state._quote = None
    elif state._quote is None and character.isspace():
        return False
    else:
        _append_token_character(state, character, index)
    return True


def _effective_command(registration: Registration, *, windows: bool) -> str:
    """Return the command active on the selected platform."""
    if windows and registration.command_windows is not None:
        return registration.command_windows
    return registration.command


def _is_unquoted_dynamic(character: str, *, word_start: bool) -> bool:
    """Return whether one unquoted character requires shell evaluation."""
    return character in {'(', ')', '|', '>', '<', ';', '&', '*', '?', '['} or (
        character == '~' and word_start
    )


def _leading_contract(
    argv: tuple[str, ...],
) -> tuple[str | None, bool, str | None] | None:
    """Parse a complete leading wrapper-option prefix conservatively."""
    index = 1
    state = _ContractState()
    valid = True
    while valid and index < len(argv) and argv[index].startswith('-'):
        option = argv[index]
        if option == '--allow-on-failure':
            valid = not state._allow_seen
            state._allow_seen = True
            index += 1
            continue
        has_value = index + 1 < len(argv) and not argv[index + 1].startswith('-')
        if option not in {'-a', '-e', '--event', '--agent', '--step-timeout'} or not has_value:
            valid = False
            continue
        valid = _consume_contract_option(state, option, argv[index + 1])
        index += 2
    if not valid or index >= len(argv):
        return None
    return state._host, state._event_seen, state._event


def _next_command_token(command: str, start: int) -> tuple[_CommandToken | None, int]:
    """Read one quote- and escape-aware token without normalizing source text."""
    index = start
    while index < len(command) and command[index].isspace():
        index += 1
    if index == len(command):
        return None, index

    token_start = index
    leading_quote = command[index] if command[index] in {"'", '"'} else None
    state = _CommandLexState(
        _equals=None,
        _escaped=False,
        _logical=[],
        _outer_quote_end=None,
        _quote=None,
        _value_start=None,
    )
    while index < len(command) and _consume_token_character(
        state,
        command[index],
        index,
        leading_quote=leading_quote,
    ):
        index += 1
    if state._escaped:
        state._logical.append('\\')

    content_end = index - (1 if state._outer_quote_end == index - 1 else 0)
    value_start = None if state._value_start is None else min(state._value_start, content_end)
    return (
        _CommandToken(
            _start=token_start,
            _end=index,
            _content_end=content_end,
            _text=''.join(state._logical),
            _value_start=value_start,
        ),
        index,
    )


def _normalized_secret_name(name: str) -> str:
    """Fold separator spellings before secret-marker comparison."""
    return name.casefold().replace('_', '-').replace('.', '-')


def _is_secret_assignment_name(name: str) -> bool:
    """Return whether one non-option assignment name carries a secret."""
    if name.startswith('--') or any(character.isspace() for character in name):
        return False
    folded = _normalized_secret_name(name)
    return any(
        marker in folded
        for marker in ('api-key', 'apikey', 'credential', 'password', 'secret', 'token')
    )


def _is_secret_option(name: str) -> bool:
    """Return whether one complete long-option name carries a secret."""
    if not name.startswith('--'):
        return False
    segments = _normalized_secret_name(name[2:]).split('-')
    if not segments or segments[0] == 'no':
        return False
    stems = [
        segment[:-1] if len(segment) > 1 and segment.endswith('s') else segment
        for segment in segments
    ]
    if any(stem in _SECRET_SEGMENTS for stem in stems):
        return True
    return any(first == 'api' and second == 'key' for first, second in pairwise(stems))


def _redact_command_text(command: str) -> str:
    """Redact secret value spans found by one bounded lexical pass."""
    spans: list[tuple[int, int]] = []
    pending_secret_option = False
    position = 0
    while True:
        token, position = _next_command_token(command, position)
        if token is None:
            break
        if pending_secret_option:
            pending_secret_option = False
            if not token._text.startswith('-'):
                spans.append((token._start, token._end))
                continue

        if token._value_start is None:
            pending_secret_option = _is_secret_option(token._text)
            continue

        name = token._text.partition('=')[0]
        if _is_secret_option(name) or _is_secret_assignment_name(name):
            spans.append((token._value_start, token._content_end))

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.extend((command[cursor:start], '[REDACTED]'))
        cursor = end
    parts.append(command[cursor:])
    return ''.join(parts)


def _static_posix_command(command: str) -> str | None:
    """Return the statically inspectable prefix, or none for dynamic syntax."""
    if _ENVIRONMENT_PREFIX.match(command) or _NEGATION_PREFIX.match(command):
        return None
    comment_start: int | None = None
    quote: str | None = None
    escaped = False
    word_start = True
    for index, character in enumerate(command):
        if character == '\n':
            return None
        if escaped or comment_start is not None:
            escaped = False
            word_start = False
            continue
        if character == '\\' and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'} and quote in {None, character}:
            quote = character if quote is None else None
            word_start = False
            continue
        if character == '#' and quote is None and word_start:
            comment_start = index
            continue
        if (character in {'$', '`'} and quote != "'") or (
            quote is None and _is_unquoted_dynamic(character, word_start=word_start)
        ):
            return None
        word_start = quote is None and character.isspace()
    return command[:comment_start]


def _wrapper_contract_error(argv: tuple[str, ...]) -> ContractError | None:
    """Classify only safely recognizable wrapper contract failures."""
    contract = _leading_contract(argv)
    if contract is None:
        return None
    host, event_seen, event = contract
    hosts = host_requirements()
    category: ContractError | None = None
    if host is None:
        category = ContractError.HOST_MISSING
    elif host not in hosts:
        category = ContractError.WRONG_HOST
    elif hosts[host].requires_event:
        if not event_seen:
            category = ContractError.EVENT_MISSING
        elif event not in hosts[host].events:
            category = ContractError.WRONG_EVENT
    elif event_seen:
        category = ContractError.EVENT_FORBIDDEN
    return category


def _wrapper_name(executable: str, *, windows: bool) -> str:
    """Return the platform-correct executable basename."""
    if windows:
        return PureWindowsPath(executable).name.casefold()
    return Path(executable).name


def _inspect_argv(argv: tuple[str, ...], *, windows: bool) -> InspectedCommand:
    """Inspect one exact argument vector."""
    if not argv or not argv[0]:
        return InspectedCommand(
            argv=argv or None,
            invocation=None,
            dynamic=False,
            error='registration has no executable',
        )
    if _wrapper_name(argv[0], windows=windows) not in _WRAPPER_NAMES:
        return InspectedCommand(argv=argv, invocation=None, dynamic=False, error=None)
    try:
        invocation = parse_hook_argv(argv[1:])
    except InvocationError as error:
        return InspectedCommand(
            argv=argv,
            invocation=None,
            dynamic=False,
            error=str(error),
            contract_error=_wrapper_contract_error(argv),
        )
    return InspectedCommand(argv=argv, invocation=invocation, dynamic=False, error=None)


def _inspect_direct(
    registration: Registration,
    command: str,
    *,
    windows: bool,
) -> InspectedCommand:
    """Inspect one loader-verified direct argument vector."""
    if registration.args is None:
        return InspectedCommand(
            argv=None,
            invocation=None,
            dynamic=False,
            error='direct argv registration is missing args',
        )
    return _inspect_argv((command, *registration.args), windows=windows)


def _inspect_posix_shell(command: str) -> InspectedCommand:
    """Inspect loader-verified static POSIX shell text."""
    static_command = _static_posix_command(command)
    if static_command is None:
        return InspectedCommand(
            argv=None,
            invocation=None,
            dynamic=True,
            error='command contains dynamic shell syntax',
        )
    try:
        argv = tuple(shlex.split(static_command, posix=True))
    except ValueError:
        return InspectedCommand(
            argv=None,
            invocation=None,
            dynamic=False,
            error='command has malformed POSIX quoting',
        )
    return _inspect_argv(argv, windows=False)


def inspect_command(
    registration: Registration,
    *,
    windows: bool,
) -> InspectedCommand:
    """Inspect one literal registration without evaluating it.

    Args:
        registration: Host registration to inspect.
        windows: Whether to apply the registration's Windows launch semantics.

    Returns:
        Static command inspection or a bounded reason it cannot be inspected.
    """
    if windows:
        if registration.windows_launch_semantics is None:
            return InspectedCommand(
                argv=None,
                invocation=None,
                dynamic=False,
                error='registration has no documented Windows launch form',
            )
        semantics = registration.windows_launch_semantics
        command = _effective_command(registration, windows=True)
    else:
        semantics = registration.launch_semantics
        command = _effective_command(registration, windows=False)

    if semantics is LaunchSemantics.DIRECT_ARGV:
        return _inspect_direct(registration, command, windows=windows)
    if semantics is LaunchSemantics.POSIX_SHELL:
        return _inspect_posix_shell(command)
    return InspectedCommand(
        argv=None,
        invocation=None,
        dynamic=False,
        error=f'{semantics.value} launch semantics cannot be inspected statically',
    )


def is_windows_remote_executable(executable: str) -> bool:
    """Identify explicit Windows UNC or device tokens without filesystem access."""
    return executable.replace('/', '\\').startswith(('\\\\', '\\??\\'))


def redact_command(registration: Registration, *, windows: bool = False) -> str:
    """Return a safe registration summary for display.

    Args:
        registration: Host registration whose effective command is displayed.
        windows: Whether to select the registration's Windows command.

    Returns:
        Redacted and length-bounded command summary.
    """
    command = _effective_command(registration, windows=windows)
    if windows and is_windows_remote_executable(command):
        return '[REDACTED] remote or device command'
    if _ENVIRONMENT_PREFIX.match(command):
        return '[REDACTED] environment command'

    redacted = _redact_command_text(command)
    if len(redacted) > _SUMMARY_LIMIT:
        return f'{redacted[: _SUMMARY_LIMIT - 3]}...'
    return redacted
