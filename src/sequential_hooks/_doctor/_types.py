"""Define the doctor report types."""

from dataclasses import dataclass
from pathlib import Path

from sequential_hooks.hosts import ConfigSource, Finding, Registration


@dataclass(frozen=True)
class PlatformReport:
    """Describe an immutable static containment probe.

    Args:
        interpreter: Running Python implementation name.
        containment: Selected containment-platform classification.
        job_object_configurable: Whether Windows Job Object containment can be
            configured, or `None` when inapplicable.
        limitations: Bounded containment limitations in report order.

    Attributes:
        interpreter: Running Python implementation name.
        containment: Selected containment-platform classification.
        job_object_configurable: Whether Windows Job Object containment can be
            configured, or `None` when inapplicable.
        limitations: Bounded containment limitations in report order.
    """

    interpreter: str
    containment: str
    job_object_configurable: bool | None
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class DoctorReport:
    """Carry one immutable, ordered host inspection report.

    Args:
        host: Host configuration contract that was inspected.
        executable: Resolved wrapper path when found, including an unsupported
            shim.
        version: Installed package version, `0.0.0` without installed metadata,
            or `None` when unavailable.
        platform: Static containment probe result.
        sources: Configuration sources in discovery order.
        registrations: Accepted registrations in loader order.
        findings: Source problems first, then findings grouped by registration,
            then cross-registration and aggregate findings.
        limitations: Host inspection limitations in report order.

    Attributes:
        host: Host configuration contract that was inspected.
        executable: Resolved wrapper path when found, including an unsupported
            shim.
        version: Installed package version, `0.0.0` without installed metadata,
            or `None` when unavailable.
        platform: Static containment probe result.
        sources: Configuration sources in discovery order.
        registrations: Accepted registrations in loader order.
        findings: Source problems first, then findings grouped by registration,
            then cross-registration and aggregate findings.
        limitations: Host inspection limitations in report order.
    """

    host: str
    executable: Path | None
    version: str | None
    platform: PlatformReport
    sources: tuple[ConfigSource, ...]
    registrations: tuple[Registration, ...]
    findings: tuple[Finding, ...]
    limitations: tuple[str, ...]
