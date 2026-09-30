# pyright: reportPrivateUsage=false

"""Implement command-line entry points for hooks and doctor checks."""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from sequential_hooks._arguments import HookInvocation, InvocationError, parse_hook_argv
from sequential_hooks._cleanup import CleanupState
from sequential_hooks._diagnostics import Diagnostics
from sequential_hooks._downstream import closable_stdout, is_downstream_closure, silence_descriptor
from sequential_hooks._executor import ForwardedSignal, ProcessExecutor
from sequential_hooks._registry import HostLoadError, load_host
from sequential_hooks._runner import run_pipeline
from sequential_hooks.hosts import (
    FinalResponse,
    HostAdapter,
    HostInputError,
    HostSpec,
    JsonObject,
)
from sequential_hooks.hosts._json import JsonProtocolError, load_object

_DOCTOR_HELP = """usage: sequential-hooks doctor --agent NAME
       sequential-hooks doctor --help

Check coding-agent configuration and platform containment support.

options:
  -a, --agent NAME          registered coding agent
  -h, --help
"""
_HELP = """usage: sequential-hooks --agent NAME [OPTIONS] CHILD [ARGS] [-- CHILD [ARGS]]...
       sequential-hooks doctor --help

Compose one through three hook commands in order.

options:
  -a, --agent NAME          registered coding agent
  -e, --event EVENT         required when the agent payload carries no event
  --step-timeout SECONDS
  --allow-on-failure
  -h, --help
  --version

The exact -- token separates child commands and is unavailable to a child.
"""
_INTERNAL_FAILURE_DIAGNOSTIC = 'unexpected internal failure'
_INVALID_INPUT_DIAGNOSTIC = 'native hook input is invalid'
_INVALID_INVOCATION_DIAGNOSTIC = 'wrapper invocation is invalid'
_LAST_RESORT_EXIT = 2
_VALID_DOCTOR_ARGUMENT_COUNT = 3


@dataclass(frozen=True)
class _NativeContext:
    """Carry one parsed native input and its trusted event."""

    _event: str
    _raw_input: bytes
    _payload: JsonObject


class _NoTrustedEventError(Exception):
    """Report that no event-specific native response can be produced."""


def _doctor_system_root(
    cwd: Path,
    *,
    windows: bool,
    environment: Mapping[str, str],
) -> Path:
    """Return the filesystem root used for machine-managed sources."""
    if not windows:
        return Path(cwd.anchor)
    if system_drive := environment.get('SystemDrive'):
        anchor = PureWindowsPath(f'{system_drive}\\').anchor
        if anchor:
            return Path(anchor)
    for variable in ('SystemRoot', 'ProgramFiles'):
        location = environment.get(variable)
        if not location:
            continue
        anchor = PureWindowsPath(location).anchor
        if anchor:
            return Path(anchor)
    return Path(cwd.anchor)


def _explicit_event(spec: HostSpec, event: str | None) -> str:
    """Return the trusted `--event` for a host that requires one."""
    if event is None:
        raise _NoTrustedEventError(f'--event is required for host {spec.name}')
    if event not in spec.events:
        raise _NoTrustedEventError(f'host {spec.name} does not serve event {event!r}')
    return event


def _is_text_command(arguments: tuple[str, ...]) -> bool:
    """Identify commands whose output is text rather than hook responses."""
    return arguments in {('-h',), ('--help',), ('--version',)} or bool(
        arguments and arguments[0] == 'doctor'
    )


def _last_resort(message: str, diagnostics: Diagnostics) -> int:
    """Answer with one diagnostic line, visible controls, and exit 2."""
    diagnostics.write_global(message.encode('unicode_escape').decode('ascii'))
    return _LAST_RESORT_EXIT


def _native_context(
    spec: HostSpec,
    event: str | None,
    raw_input: bytes,
) -> _NativeContext:
    """Parse native input and bind the trusted event for the selected host."""
    # Invalid JSON and unresolved host events remain distinct so callers can
    # select the documented configuration or last-resort response.
    if spec.requires_event:
        trusted = _explicit_event(spec, event)
        return _NativeContext(trusted, raw_input, load_object(raw_input))
    payload = load_object(raw_input)
    failure = CleanupState()
    payload_event: str | None = None
    with failure:
        payload_event = spec.payload_event(payload)
    if failure.first_error is not None:
        raise _NoTrustedEventError(
            f'host {spec.name} event resolution failed: {type(failure.first_error).__name__}'
        ) from failure.first_error
    if payload_event is None or payload_event not in spec.events:
        raise _NoTrustedEventError('native event could not be established')
    return _NativeContext(payload_event, raw_input, payload)


def _run_doctor_cli(
    arguments: tuple[str, ...],
    diagnostics: Diagnostics,
) -> int:
    """Parse and run the isolated doctor command grammar."""
    if arguments in {('doctor', '-h'), ('doctor', '--help')}:
        sys.stdout.write(_DOCTOR_HELP)
        return 0
    if (
        len(arguments) != _VALID_DOCTOR_ARGUMENT_COUNT
        or arguments[1] not in {'-a', '--agent'}
        or not arguments[2]
        or arguments[2].startswith('-')
    ):
        diagnostics.write_global('invalid doctor invocation; use doctor --help')
        return 2
    host = arguments[2]
    home = Path.home()
    cwd = Path.cwd()
    from sequential_hooks._doctor import run_doctor  # noqa: PLC0415

    return run_doctor(
        host,
        home=home,
        cwd=cwd,
        system_root=_doctor_system_root(
            cwd,
            windows=sys.platform == 'win32',
            environment=os.environ,
        ),
        path_value=os.environ.get('PATH'),
        path_ext=os.environ.get('PATHEXT'),
        stream=sys.stdout,
    )


def _trusted_event_for_error(
    spec: HostSpec,
    event: str | None,
    stdin: BinaryIO,
) -> str:
    """Establish the event a configuration response may answer."""
    if spec.requires_event:
        return _explicit_event(spec, event)
    try:
        return _native_context(spec, event, stdin.read())._event
    except JsonProtocolError as error:
        raise _NoTrustedEventError('native event could not be established') from error


def _version() -> str | None:
    """Return the installed version, or none when metadata is unavailable."""
    import importlib.metadata  # noqa: PLC0415

    try:
        value = importlib.metadata.version('sequential-hooks')
    except importlib.metadata.PackageNotFoundError:
        return None
    return f'sequential-hooks {value}\n'


def _run_text_command(arguments: tuple[str, ...], diagnostics: Diagnostics) -> int:
    """Write top-level text or dispatch the doctor command."""
    if arguments in {('-h',), ('--help',)}:
        sys.stdout.write(_HELP)
        return 0
    if arguments == ('--version',):
        version = _version()
        if version is None:
            diagnostics.write_global(
                'installed distribution metadata for sequential-hooks is unavailable'
            )
            return 2
        sys.stdout.write(version)
        return 0
    return _run_doctor_cli(arguments, diagnostics)


def _write_response(stdout: BinaryIO, response: bytes) -> bool:
    """Write one response, returning false after a closed downstream pipe."""
    try:
        stdout.write(response)
        stdout.flush()
    except OSError as error:
        if not is_downstream_closure(error):
            raise
        silence_descriptor(sys.stdout)
        return False
    return True


def _configuration_response(
    spec: HostSpec,
    event: str,
    message: str,
    diagnostics: Diagnostics,
    stdout: BinaryIO,
) -> int:
    """Resolve the host response before committing wrapper diagnostics."""
    failure = CleanupState()
    response: FinalResponse | None = None
    response_exit_code = 0
    response_stdout = b''
    with failure:
        response = spec.configuration_response(event, message, diagnostics)
    if response is not None:
        with failure:
            response_stdout = response.stdout
            response_exit_code = response.exit_code
    if failure.first_error is not None:
        return _last_resort(
            f'{message}; host {spec.name} configuration response failed: '
            f'{type(failure.first_error).__name__}',
            diagnostics,
        )
    if response is None:
        return _last_resort(
            f'{message}; host {spec.name} returned no configuration response', diagnostics
        )
    diagnostics.write_global(message)
    _write_response(stdout, response_stdout)
    return response_exit_code


def _handle_parse_error(
    error: InvocationError,
    stdin: BinaryIO,
    stdout: BinaryIO,
    diagnostics: Diagnostics,
) -> int:
    """Translate syntax only after a host loads and its event is trusted."""
    if error.host is None:
        return _last_resort(str(error), diagnostics)
    try:
        spec = load_host(error.host)
    except HostLoadError as load_error:
        return _last_resort(f'{error}; {load_error}', diagnostics)
    try:
        event = _trusted_event_for_error(spec, error.event, stdin)
    except _NoTrustedEventError as event_error:
        return _last_resort(f'{error}; {event_error}', diagnostics)
    return _configuration_response(
        spec,
        event,
        _INVALID_INVOCATION_DIAGNOSTIC,
        diagnostics,
        stdout,
    )


def _prepare_adapter(
    spec: HostSpec,
    invocation: HookInvocation,
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    diagnostics: Diagnostics,
) -> HostAdapter | int:
    """Read native input and construct the adapter, or answer a failure tier."""
    raw_input = stdin.read()
    try:
        native_context = _native_context(spec, invocation.event, raw_input)
    except _NoTrustedEventError as error:
        return _last_resort(str(error), diagnostics)
    except JsonProtocolError:
        if invocation.event is None:
            return _last_resort('native event could not be established', diagnostics)
        return _configuration_response(
            spec,
            invocation.event,
            _INVALID_INPUT_DIAGNOSTIC,
            diagnostics,
            stdout,
        )
    failure = CleanupState()
    adapter: HostAdapter | None = None
    with failure:
        adapter = spec.adapter(
            native_context._event,
            native_context._raw_input,
            native_context._payload,
            diagnostics,
            allow_on_failure=invocation.allow_on_failure,
        )
    if isinstance(failure.first_error, HostInputError):
        return _configuration_response(
            spec,
            native_context._event,
            f'{_INVALID_INPUT_DIAGNOSTIC}: {failure.first_error}',
            diagnostics,
            stdout,
        )
    if failure.first_error is not None or adapter is None:
        return _last_resort(
            f'host {spec.name} adapter failed: {type(failure.first_error).__name__}',
            diagnostics,
        )
    return adapter


def _run_chain(
    invocation: HookInvocation,
    *,
    adapter: HostAdapter,
    stdout: BinaryIO,
    diagnostics: Diagnostics,
) -> int:
    """Run the child chain and write the adapter's response."""
    executor = ProcessExecutor(diagnostics)
    failure = CleanupState()
    response: FinalResponse | None = None
    response_exit_code = 0
    response_stdout = b''
    try:
        with executor.signal_forwarding(), failure:
            response = run_pipeline(
                adapter,
                invocation.steps,
                executor,
                invocation.step_timeout,
                allow_on_failure=invocation.allow_on_failure,
            )
            response_stdout = response.stdout
            response_exit_code = response.exit_code
    except ForwardedSignal as error:
        return 128 + error.signum
    if failure.first_error is not None or response is None:
        return _last_resort(_INTERNAL_FAILURE_DIAGNOSTIC, diagnostics)
    _write_response(stdout, response_stdout)
    return response_exit_code


def _run_hook(
    invocation: HookInvocation,
    stdin: BinaryIO,
    stdout: BinaryIO,
    diagnostics: Diagnostics,
) -> int:
    """Run one parsed hook invocation through the three failure tiers."""
    try:
        spec = load_host(invocation.host)
    except HostLoadError as error:
        return _last_resort(str(error), diagnostics)
    if not spec.requires_event and invocation.event is not None:
        message = f'--event is not accepted for host {spec.name}'
        try:
            event = _trusted_event_for_error(spec, invocation.event, stdin)
        except _NoTrustedEventError as error:
            return _last_resort(f'{message}; {error}', diagnostics)
        return _configuration_response(spec, event, message, diagnostics, stdout)
    try:
        if spec.requires_event:
            _explicit_event(spec, invocation.event)
    except _NoTrustedEventError as error:
        return _last_resort(str(error), diagnostics)
    prepared = _prepare_adapter(
        spec,
        invocation,
        stdin=stdin,
        stdout=stdout,
        diagnostics=diagnostics,
    )
    if isinstance(prepared, int):
        return prepared
    return _run_chain(
        invocation,
        adapter=prepared,
        stdout=stdout,
        diagnostics=diagnostics,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface using the process standard streams.

    Hook mode exchanges native bytes; help, version, and doctor write text to
    stdout. Downstream closure is absorbed while preserving the command status.

    Args:
        argv: Tokens after the executable name, or `None` for `sys.argv[1:]`.

    Returns:
        Process exit status.

    Raises:
        OSError: If writing, flushing, or redirecting output fails for a reason
            other than downstream closure. A diagnostic `OSError` raised during
            pipeline execution is translated under the internal-failure policy.
    """
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    diagnostics = Diagnostics(sys.stderr)
    if _is_text_command(arguments):
        with closable_stdout():
            return _run_text_command(arguments, diagnostics)
    try:
        invocation = parse_hook_argv(arguments)
    except InvocationError as error:
        return _handle_parse_error(error, sys.stdin.buffer, sys.stdout.buffer, diagnostics)
    return _run_hook(invocation, sys.stdin.buffer, sys.stdout.buffer, diagnostics)
