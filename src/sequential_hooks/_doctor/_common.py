# pyright: reportPrivateUsage=false

"""Analyze shared doctor registration and platform contracts."""

import math
import platform
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sequential_hooks._containment import probe_current_containment
from sequential_hooks._doctor._command import is_windows_remote_executable
from sequential_hooks._doctor._types import (
    ContractError,
    Finding,
    FindingSeverity,
    InspectedCommand,
    PlatformReport,
    Registration,
)
from sequential_hooks._executable import ExecutableStatus, resolve_executable
from sequential_hooks.hosts import HostSpec

_ALL_MATCHERS = frozenset({'', '*', '.*'})
_MINIMUM_OUTER_TIMEOUT = 5.0
_SAFE_SHIM_BASENAME_LIMIT = 40


@dataclass(frozen=True)
class _ExecutableEnvironment:
    """Describe the environment used to resolve inspected executables."""

    _path_ext: str | None
    _path_value: str | None
    _windows: bool


def _expected_contract_category(
    category: ContractError,
    *,
    requires_event: bool,
) -> ContractError:
    """Prioritize the expected event-requiring host over an embedded host's event rule."""
    if requires_event and category is ContractError.EVENT_FORBIDDEN:
        return ContractError.WRONG_HOST
    return category


def _finding(
    severity: FindingSeverity,
    code: str,
    message: str,
    correction: str | None = None,
) -> Finding:
    """Build one bounded finding."""
    return Finding(severity, code, message, correction)


def _contract_finding(
    category: ContractError,
    spec: HostSpec,
    registration: Registration,
) -> Finding:
    """Build shared wrapper-contract text for complete and partial invocations."""
    corrections = {
        ContractError.EVENT_FORBIDDEN: (f'Use --agent {spec.name} and remove the --event option.'),
        ContractError.EVENT_MISSING: f'Use --event {registration.event}.',
        ContractError.HOST_MISSING: f'Use --agent {spec.name}.',
        ContractError.WRONG_EVENT: f'Use --event {registration.event}.',
        ContractError.WRONG_HOST: f'Use --agent {spec.name}.',
    }
    messages = {
        ContractError.EVENT_FORBIDDEN: 'wrapper event is not accepted for this agent',
        ContractError.EVENT_MISSING: 'wrapper event is missing',
        ContractError.HOST_MISSING: 'wrapper agent is missing',
        ContractError.WRONG_EVENT: 'wrapper event does not match the registered event',
        ContractError.WRONG_HOST: 'wrapper agent does not match the configured agent',
    }
    return _finding(
        FindingSeverity.ERROR,
        category.value,
        messages[category],
        corrections[category],
    )


def _invocation_contract(
    spec: HostSpec,
    registration: Registration,
    invocation_host: str,
    invocation_event: str | None,
) -> tuple[Finding, ...]:
    """Analyze one wrapper agent and event contract."""
    findings: list[Finding] = []
    if invocation_host != spec.name:
        findings.append(_contract_finding(ContractError.WRONG_HOST, spec, registration))
    if registration.event not in spec.events:
        findings.append(
            _finding(
                FindingSeverity.ERROR,
                'native_event_unsupported',
                'wrapper is registered under a native event it cannot serve',
                f'Register the sequential-hooks wrapper only under {", ".join(spec.events)}.',
            )
        )
        return tuple(findings)
    if spec.requires_event and invocation_event is None:
        findings.append(_contract_finding(ContractError.EVENT_MISSING, spec, registration))
    elif spec.requires_event and invocation_event != registration.event:
        findings.append(_contract_finding(ContractError.WRONG_EVENT, spec, registration))
    elif not spec.requires_event and invocation_event is not None:
        findings.append(_contract_finding(ContractError.EVENT_FORBIDDEN, spec, registration))
    return tuple(findings)


def _matcher_overlap(first: str | None, second: str | None) -> str:
    """Classify a matcher pair without evaluating either expression."""
    if first == second or first is None or second is None:
        return 'definite'
    if first in _ALL_MATCHERS or second in _ALL_MATCHERS:
        return 'definite'
    return 'possible'


def _registration_state(spec: HostSpec, registration: Registration) -> tuple[Finding, ...]:
    """Analyze one registration's enabled and outer-timeout state."""
    findings: list[Finding] = []
    timeout = cast('object', registration.timeout)
    if registration.enabled is False:
        findings.append(
            _finding(FindingSeverity.INFO, 'registration_disabled', 'registration is disabled')
        )
    elif registration.enabled is None:
        findings.append(
            _finding(
                FindingSeverity.WARNING,
                'enabled_unknown',
                'registration enabled state cannot be inspected statically',
                (
                    'Reconcile disableAllHooks values in the discovered Claude settings sources.'
                    if spec.name == 'claude'
                    else 'Reconcile hook activation settings in the discovered sources.'
                ),
            )
        )
    if timeout is None:
        findings.append(
            _finding(
                FindingSeverity.WARNING,
                'outer_timeout_missing',
                'outer handler timeout is missing',
                f'Set an outer handler timeout of at least {_MINIMUM_OUTER_TIMEOUT:g} seconds.',
            )
        )
    elif not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        findings.append(
            _finding(
                FindingSeverity.ERROR,
                'outer_timeout_malformed',
                'outer handler timeout is not a positive finite number',
                f'Set an outer handler timeout of at least {_MINIMUM_OUTER_TIMEOUT:g} seconds.',
            )
        )
    elif timeout < _MINIMUM_OUTER_TIMEOUT:
        findings.append(
            _finding(
                FindingSeverity.WARNING,
                'outer_timeout_too_small',
                'outer handler timeout may expire before cleanup completes',
                f'Set an outer handler timeout of at least {_MINIMUM_OUTER_TIMEOUT:g} seconds.',
            )
        )
    return tuple(findings)


def _safe_shim_path(resolved: Path | None, *, wrapper: bool) -> str:
    """Return a bounded shim identifier without directory disclosure."""
    if resolved is None:
        return '[redacted]/[shim]'
    name = resolved.name
    suffix = resolved.suffix.casefold()
    if wrapper and name.casefold() in {
        'sequential-hooks.bat',
        'sequential-hooks.cmd',
        'sequential-hooks.ps1',
    }:
        basename = name[:_SAFE_SHIM_BASENAME_LIMIT]
    else:
        basename = f'[shim]{suffix}' if suffix in {'.bat', '.cmd', '.ps1'} else '[shim]'
    return f'[redacted]/{basename}'


def _analyze_executable(
    executable: str,
    environment: _ExecutableEnvironment,
    *,
    wrapper: bool,
) -> Finding | None:
    """Return one executable resolution finding, if needed."""
    if environment._windows and is_windows_remote_executable(executable):
        return _finding(
            FindingSeverity.WARNING,
            'command_uninspectable',
            'remote or device executable is not inspected statically',
            'Use a local executable path for static inspection; '
            'verify the remote or device command separately.',
        )
    resolution = resolve_executable(
        executable,
        path_value=environment._path_value,
        windows=environment._windows,
        pathext_value=environment._path_ext,
    )
    label = 'wrapper' if wrapper else 'child'
    if resolution.status is ExecutableStatus.NOT_FOUND:
        return _finding(
            FindingSeverity.ERROR,
            f'{label}_executable_missing',
            f'{label} executable cannot be resolved',
            f'Install or configure a runnable {label} executable.',
        )
    if resolution.status is ExecutableStatus.UNSUPPORTED_SHIM:
        safe_path = _safe_shim_path(resolution.resolved, wrapper=wrapper)
        return _finding(
            FindingSeverity.ERROR,
            f'{label}_executable_unsupported',
            f'{label} executable resolves to an unsupported Windows shim at {safe_path}',
            'Replace the Windows batch shim with a real .exe or .com executable entry point.',
        )
    return None


def _analyze_invocation(
    spec: HostSpec,
    registration: Registration,
    inspected: InspectedCommand,
    environment: _ExecutableEnvironment,
) -> tuple[Finding, ...]:
    """Analyze one statically inspected registration."""
    findings: list[Finding] = []
    if inspected.dynamic:
        findings.append(
            _finding(
                FindingSeverity.WARNING,
                'command_dynamic',
                'dynamic command cannot be judged healthy statically',
                'Use a loader-documented literal launch form.',
            )
        )
        return tuple(findings)
    if inspected.contract_error is not None:
        category = _expected_contract_category(
            inspected.contract_error,
            requires_event=spec.requires_event,
        )
        findings.append(_contract_finding(category, spec, registration))
        return tuple(findings)
    if inspected.error is not None:
        findings.append(
            _finding(
                FindingSeverity.WARNING,
                'command_uninspectable',
                'registration command cannot be inspected statically',
                'Use a loader-documented literal launch form '
                'and check the wrapper argument grammar.',
            )
        )
        return tuple(findings)
    if inspected.argv is None:
        return tuple(findings)

    wrapper = inspected.invocation is not None
    executable_finding = _analyze_executable(
        inspected.argv[0],
        environment,
        wrapper=wrapper,
    )
    if executable_finding is not None:
        findings.append(executable_finding)
    if inspected.invocation is None:
        return tuple(findings)

    invocation = inspected.invocation
    findings.extend(
        _invocation_contract(
            spec,
            registration,
            invocation.host,
            invocation.event,
        )
    )
    for step in invocation.steps:
        child_finding = _analyze_executable(
            step.argv[0],
            environment,
            wrapper=False,
        )
        if child_finding is not None:
            findings.append(child_finding)
    if invocation.step_timeout is None:
        findings.append(
            _finding(
                FindingSeverity.INFO,
                'outer_timeout_only',
                'no internal step timeout is configured; only the outer handler timeout applies',
                'Add --step-timeout if each child needs an independent bound.',
            )
        )
    return tuple(findings)


def _same_child_argv(
    first: tuple[str, ...],
    second: tuple[str, ...],
    *,
    windows: bool,
) -> bool:
    """Compare child vectors using only executable platform folding."""
    if not windows:
        return first == second
    return first[0].casefold() == second[0].casefold() and first[1:] == second[1:]


def analyze_registration(
    spec: HostSpec,
    inspected_registration: tuple[Registration, InspectedCommand],
    *,
    path_value: str | None,
    path_ext: str | None,
    windows: bool,
) -> tuple[Finding, ...]:
    """Return state and invocation findings for one inspected registration.

    Args:
        spec: Loaded host owning the event domain and event option rule.
        inspected_registration: Registration and its existing command
            inspection.
        path_value: Search path used to resolve executables.
        path_ext: Windows executable extensions used during resolution.
        windows: Whether to use Windows launch and executable semantics.

    Returns:
        Registration state findings followed by invocation findings.
    """
    registration, inspected = inspected_registration
    environment = _ExecutableEnvironment(
        _path_ext=path_ext,
        _path_value=path_value,
        _windows=windows,
    )
    return (
        *_registration_state(spec, registration),
        *_analyze_invocation(
            spec,
            registration,
            inspected,
            environment,
        ),
    )


def analyze_registration_pairs(
    inspected_registrations: Iterable[tuple[Registration, InspectedCommand]],
    *,
    windows: bool,
) -> tuple[Finding, ...]:
    """Return cross-registration findings from existing command inspections.

    Args:
        inspected_registrations: Registrations and inspections in loader order.
        windows: Whether executable comparisons use Windows semantics.

    Returns:
        Findings for each earlier registration paired with each later one,
        preserving input order.
    """
    inspected_registrations = tuple(inspected_registrations)
    findings: list[Finding] = []
    for first_index, (first, first_inspected) in enumerate(inspected_registrations):
        for second_index in range(first_index + 1, len(inspected_registrations)):
            second, second_inspected = inspected_registrations[second_index]
            if first.event != second.event:
                continue
            if first_inspected.invocation is not None and second_inspected.invocation is not None:
                if first.matcher == second.matcher:
                    findings.append(
                        _finding(
                            FindingSeverity.WARNING,
                            'duplicate_wrapper',
                            'event has repeated wrapper registrations',
                            'Keep one wrapper registration for each event and matcher.',
                        )
                    )
                overlap = _matcher_overlap(first.matcher, second.matcher)
                findings.append(
                    _finding(
                        FindingSeverity.WARNING if overlap == 'definite' else FindingSeverity.INFO,
                        f'{overlap}_matcher_overlap',
                        f'{overlap} same-event matcher overlap',
                        'Make same-event matcher scopes disjoint.',
                    )
                )
            wrapper, literal = (
                (first_inspected, second_inspected)
                if first_inspected.invocation is not None
                else (second_inspected, first_inspected)
            )
            if (
                wrapper.invocation is None
                or literal.invocation is not None
                or literal.argv is None
            ):
                continue
            if any(
                _same_child_argv(step.argv, literal.argv, windows=windows)
                for step in wrapper.invocation.steps
            ):
                findings.append(
                    _finding(
                        FindingSeverity.WARNING,
                        'duplicate_child_registration',
                        'literal child duplicates a wrapper step',
                        'Remove the separately registered literal child.',
                    )
                )
    return tuple(findings)


def inspect_platform() -> PlatformReport:
    """Report interpreter and static containment availability.

    Returns:
        Current interpreter, containment classification, configuration result,
        and bounded limitations.
    """
    interpreter = platform.python_implementation()
    containment, configurable, limitations = probe_current_containment(
        implementation=interpreter,
        platform_name=sys.platform,
    )
    return PlatformReport(
        interpreter,
        containment.value,
        configurable,
        limitations,
    )
