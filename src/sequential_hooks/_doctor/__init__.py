"""Orchestrate deterministic, read-only host diagnostics."""

import importlib.metadata
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path, PurePath, PureWindowsPath
from typing import Protocol

from sequential_hooks._doctor._command import inspect_command, redact_command
from sequential_hooks._doctor._common import (
    analyze_registration,
    analyze_registration_pairs,
    inspect_platform,
)
from sequential_hooks._doctor._types import DoctorReport, PlatformReport
from sequential_hooks._executable import ExecutableStatus, resolve_executable
from sequential_hooks._registry import HostLoadError, load_host
from sequential_hooks.hosts import ConfigSource, Finding, FindingSeverity, Registration


class DoctorOutput(Protocol):
    """Write doctor report text."""

    def write(self, value: str, /) -> int:
        """Write report text.

        Args:
            value: Text to write.

        Returns:
            The number of characters written.

        Raises:
            OSError: If writing report text fails.
        """
        ...

    def writelines(self, lines: Iterable[str], /) -> None:
        """Write report lines.

        Args:
            lines: Report lines to write in order.

        Raises:
            OSError: If writing report lines fails.
        """
        ...


def _installed_version() -> str:
    """Return the installed package version or `0.0.0` without metadata."""
    try:
        return importlib.metadata.version('sequential-hooks')
    except importlib.metadata.PackageNotFoundError:
        return '0.0.0'


def _render_boolean(value: bool | None) -> str:
    """Render one inspectable, unknown, or inapplicable state."""
    if value is None:
        return 'unknown'
    return 'true' if value else 'false'


def _render_scalar(value: object) -> str:
    """Return one ASCII-safe line scalar without losing code-point identity."""
    rendered: list[str] = []
    for character in str(value):
        if character == '\\':
            escaped = '\\\\'
        elif ' ' <= character <= '~':
            escaped = character
        else:
            escaped = character.encode('unicode_escape').decode('ascii')
        rendered.append(escaped)
    return ''.join(rendered)


def _render_findings(findings: tuple[Finding, ...], stream: DoctorOutput) -> None:
    """Render source problems, hook groups, and cross-hook findings with exact corrections."""
    stream.write('\nfindings:\n')
    if not findings:
        stream.write('- none\n')
        return
    for finding in findings:
        stream.write(
            f'- [{_render_scalar(finding.severity.value)}] '
            f'{_render_scalar(finding.code)}: {_render_scalar(finding.message)}\n'
        )
        if finding.correction is not None:
            stream.write(f'  correction: {_render_scalar(finding.correction)}\n')


def _render_limitations(
    platform: PlatformReport,
    limitations: tuple[str, ...],
    stream: DoctorOutput,
) -> None:
    """Render platform and host limitations in stable order."""
    stream.write('\nlimitations:\n')
    combined = (*platform.limitations, *limitations)
    if not combined:
        stream.write('- none\n')
        return
    stream.writelines(f'- {_render_scalar(limitation)}\n' for limitation in combined)


def _render_path(path: PurePath) -> str:
    """Render path separators portably while preserving namespace spelling."""
    text = str(path)
    if not (
        isinstance(path, PureWindowsPath)
        and text.replace('/', '\\').startswith(('\\\\?\\', '\\\\.\\', '\\??\\'))
    ):
        text = path.as_posix()
    return _render_scalar(text)


def _render_registrations(
    registrations: tuple[Registration, ...],
    stream: DoctorOutput,
    *,
    windows: bool,
) -> None:
    """Render registrations without exposing args or unredacted commands."""
    stream.write('\nregistrations:\n')
    if not registrations:
        stream.write('- none\n')
        return
    for registration in registrations:
        matcher = registration.matcher if registration.matcher is not None else 'any'
        timeout = str(registration.timeout) if registration.timeout is not None else 'unknown'
        registration_type = registration.type if registration.type is not None else 'unknown'
        windows_launch = (
            registration.windows_launch_semantics.value
            if registration.windows_launch_semantics is not None
            else 'unavailable'
        )
        stream.write(
            f'- source: {_render_path(registration.source.path)}\n'
            f'  event: {_render_scalar(registration.event)}\n'
            f'  matcher: {_render_scalar(matcher)}\n'
            f'  command: {_render_scalar(redact_command(registration, windows=windows))}\n'
            f'  timeout: {_render_scalar(timeout)}\n'
            f'  enabled: {_render_scalar(_render_boolean(registration.enabled))}\n'
            f'  type: {_render_scalar(registration_type)}\n'
            f'  launch: {_render_scalar(registration.launch_semantics.value)}\n'
            f'  windows launch: {_render_scalar(windows_launch)}\n'
        )


def _render_sources(sources: tuple[ConfigSource, ...], stream: DoctorOutput) -> None:
    """Render every configuration source in discovery order."""
    stream.write('\nconfiguration sources:\n')
    if not sources:
        stream.write('- none\n')
        return
    for source in sources:
        state = 'exists' if source.exists else 'missing'
        stream.write(
            f'- [{_render_scalar(source.kind.value)}] {_render_path(source.path)}: {state}\n'
        )


def _render_report(report: DoctorReport, stream: DoctorOutput) -> None:
    """Render one deterministic, redaction-safe human report."""
    wrapper = _render_path(report.executable) if report.executable is not None else 'not found'
    version = report.version if report.version is not None else 'unknown'
    job_object = (
        'not applicable'
        if report.platform.job_object_configurable is None
        else _render_boolean(report.platform.job_object_configurable)
    )
    stream.write(
        'sequential-hooks doctor\n'
        'display: ASCII backslash escapes; \\\\ represents one literal backslash\n'
        f'agent: {_render_scalar(report.host)}\n'
        f'wrapper: {wrapper}\n'
        f'version: {_render_scalar(version)}\n'
        f'interpreter: {_render_scalar(report.platform.interpreter)}\n'
        f'containment: {_render_scalar(report.platform.containment)}\n'
        f'job object configurable: {_render_scalar(job_object)}\n'
    )
    _render_sources(report.sources, stream)
    _render_registrations(report.registrations, stream, windows=sys.platform == 'win32')
    _render_findings(report.findings, stream)
    _render_limitations(report.platform, report.limitations, stream)


def _wrapper_finding(
    status: ExecutableStatus,
    *,
    existing_codes: set[str],
) -> Finding | None:
    """Return one aggregate wrapper-resolution problem without duplicates."""
    if status is ExecutableStatus.NOT_FOUND and 'wrapper_executable_missing' not in existing_codes:
        return Finding(
            FindingSeverity.ERROR,
            'wrapper_executable_missing',
            'wrapper executable cannot be resolved',
            'Install or configure a runnable wrapper executable.',
        )
    if (
        status is ExecutableStatus.UNSUPPORTED_SHIM
        and 'wrapper_executable_unsupported' not in existing_codes
    ):
        return Finding(
            FindingSeverity.ERROR,
            'wrapper_executable_unsupported',
            'wrapper executable resolves to an unsupported Windows shim',
            'Replace the Windows batch shim with a real .exe or .com executable entry point.',
        )
    return None


def _report(
    report: DoctorReport,
    *,
    path_value: str | None,
    path_ext: str | None,
) -> DoctorReport:
    """Resolve the wrapper executable and complete one inspection report."""
    resolution = resolve_executable(
        'sequential-hooks',
        path_value=path_value,
        windows=sys.platform == 'win32',
        pathext_value=path_ext,
    )
    wrapper_finding = _wrapper_finding(
        resolution.status,
        existing_codes={finding.code for finding in report.findings},
    )
    findings = report.findings
    if wrapper_finding is not None:
        findings = (*findings, wrapper_finding)
    return replace(report, executable=resolution.resolved, findings=findings)


def _inspect_host(  # noqa: PLR0913
    host: str,
    *,
    home: Path,
    cwd: Path,
    system_root: Path,
    path_value: str | None,
    path_ext: str | None,
) -> DoctorReport:
    """Inspect one host without executing or editing configuration."""
    try:
        spec = load_host(host)
    except HostLoadError as error:
        unavailable = Finding(
            FindingSeverity.ERROR,
            'host_unavailable',
            str(error),
            'Install an adapter package that registers this agent in the '
            'sequential_hooks.hosts entry-point group.',
        )
        return _report(
            DoctorReport(
                host, None, _installed_version(), inspect_platform(), (), (), (unavailable,), ()
            ),
            path_value=path_value,
            path_ext=path_ext,
        )
    loader = spec.doctor_loader()
    windows = sys.platform == 'win32'
    sources = loader.discover_sources(home, cwd, system_root)
    registrations, source_findings = loader.load_registrations(sources)
    inspected_registrations = tuple(
        (registration, inspect_command(registration, windows=windows))
        for registration in registrations
    )
    findings = list(source_findings)
    for registration, inspected in inspected_registrations:
        findings.extend(registration.configuration_findings)
        findings.extend(
            analyze_registration(
                spec,
                (registration, inspected),
                path_value=path_value,
                path_ext=path_ext,
                windows=windows,
            )
        )
        findings.extend(loader.analyze_registration(registration, inspected, windows=windows))
    findings.extend(analyze_registration_pairs(inspected_registrations, windows=windows))
    return _report(
        DoctorReport(
            host,
            None,
            _installed_version(),
            inspect_platform(),
            sources,
            registrations,
            tuple(findings),
            loader.inspection_limitations(),
        ),
        path_value=path_value,
        path_ext=path_ext,
    )


def run_doctor(  # noqa: PLR0913
    host: str,
    *,
    home: Path,
    cwd: Path,
    system_root: Path,
    path_value: str | None,
    path_ext: str | None,
    stream: DoctorOutput,
) -> int:
    """Inspect, render, and return the doctor status.

    Args:
        host: Entry-point name of the host to inspect; an unloadable host is
            reported as a `host_unavailable` finding.
        home: Injected home directory used for user configuration.
        cwd: Injected working directory used for project configuration.
        system_root: Injected filesystem root used for system configuration.
        path_value: Injected executable search path, if set.
        path_ext: Injected Windows executable suffix list, if set.
        stream: Text output destination for the report.

    Returns:
        Zero when findings are informational, otherwise one.

    Raises:
        OSError: If rendering the report to `stream` fails.
    """
    report = _inspect_host(
        host,
        home=home,
        cwd=cwd,
        system_root=system_root,
        path_value=path_value,
        path_ext=path_ext,
    )
    _render_report(report, stream)
    return int(
        any(
            finding.severity in {FindingSeverity.ERROR, FindingSeverity.WARNING}
            for finding in report.findings
        )
    )
