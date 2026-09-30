"""Inspect Antigravity hook configuration sources."""

from collections.abc import Iterable
from pathlib import Path
from typing import cast

from sequential_hooks.hosts import (
    ConfigSource,
    Finding,
    FindingSeverity,
    InspectedCommand,
    JsonObjectExpectedError,
    JsonProtocolError,
    LaunchSemantics,
    Registration,
    SourceKind,
    load_object_text,
    parse_timeout,
    project_layers,
)

_DEFAULT_HANDLER_TIMEOUT = 30.0
_LIMITATIONS = (
    (
        'organization-managed Antigravity hook sources cannot be inspected until '
        'their exact readable source paths are established'
    ),
    (
        'product-internal or plugin-provided Antigravity hooks cannot be inspected until '
        'their exact readable source paths are established; plugin data is not scanned'
    ),
)
_NATIVE_EVENTS = frozenset(
    {'PostInvocation', 'PostToolUse', 'PreInvocation', 'PreToolUse', 'Stop'}
)
_WRAPPER_EVENTS = frozenset({'PostToolUse', 'PreToolUse'})


class AgyDoctorLoader:
    """Inspect the Antigravity hooks files through the published `DoctorLoader` contract."""

    def analyze_registration(
        self,
        registration: Registration,
        inspected: InspectedCommand,
        *,
        windows: bool,
    ) -> tuple[Finding, ...]:
        """Return host-specific findings for one inspected registration.

        Args:
            registration: Accepted registration.
            inspected: Static inspection of the registration's command.
            windows: Whether Windows launch semantics apply.

        Returns:
            Findings in report order.
        """
        return _analyze_registration(registration, inspected, windows=windows)

    def discover_sources(
        self,
        home: Path,
        cwd: Path,
        system_root: Path,
    ) -> tuple[ConfigSource, ...]:
        """Return every configuration source in deterministic inspection order.

        Args:
            home: Home directory used to locate user configuration.
            cwd: Working directory used to locate project configuration.
            system_root: Filesystem root used to locate system configuration.

        Returns:
            Candidate sources, existing or not, in inspection order.
        """
        return _discover_sources(home, cwd, system_root)

    def inspection_limitations(self) -> tuple[str, ...]:
        """Return configuration sources static inspection cannot establish.

        Returns:
            Stable limitation descriptions in report order.
        """
        return _inspection_limitations()

    def load_registrations(
        self,
        sources: tuple[ConfigSource, ...],
    ) -> tuple[tuple[Registration, ...], tuple[Finding, ...]]:
        """Extract registrations and structural findings in source order.

        Args:
            sources: Sources returned by `discover_sources`.

        Returns:
            Accepted registrations and source-isolated findings.
        """
        return _load_registrations(sources)


def _analyze_registration(
    registration: Registration,
    inspected: InspectedCommand,
    *,
    windows: bool,
) -> tuple[Finding, ...]:
    """Return Antigravity findings for an inspected registration."""
    if windows or registration.event not in _WRAPPER_EVENTS:
        return ()
    findings: list[Finding] = []
    if inspected.invocation is None:
        if (
            inspected.argv is not None
            and inspected.error is not None
            and inspected.contract_error is None
        ):
            findings.append(
                Finding(
                    FindingSeverity.WARNING,
                    'agy_wrapper_contract_uninspectable',
                    'Antigravity wrapper host and event cannot be inspected from '
                    'malformed literal args',
                    'Check the literal wrapper argument grammar, including '
                    '--agent agy and --event.',
                )
            )
        return tuple(findings)

    invocation = inspected.invocation
    if invocation.step_timeout is None:
        return tuple(findings)
    chain_budget = len(invocation.steps) * invocation.step_timeout
    if registration.timeout is None:
        raise AssertionError('validated registration timeout is missing')
    if chain_budget >= registration.timeout:
        findings.append(
            Finding(
                FindingSeverity.WARNING,
                'agy_chain_budget_uncontained',
                'configured sequential-hooks chain budget leaves no room within the '
                'outer Antigravity handler timeout',
                'Set the outer Antigravity handler timeout above the configured chain budget.',
            )
        )
    return tuple(findings)


def _discover_sources(
    home: Path,
    cwd: Path,
    system_root: Path,
) -> tuple[ConfigSource, ...]:
    """Return documented Antigravity files in inspection order."""
    del system_root
    paths: list[tuple[Path, SourceKind]] = [(home / '.gemini/config/hooks.json', SourceKind.USER)]
    paths.extend(
        (layer / '.agents/hooks.json', SourceKind.PROJECT) for layer in project_layers(cwd)
    )
    return tuple(ConfigSource(path, kind, path.exists()) for path, kind in paths)


def _finding(
    source: ConfigSource,
    code: str,
    message: str,
    correction: str | None = None,
) -> Finding:
    """Build a source-specific structural finding."""
    return Finding(
        FindingSeverity.WARNING,
        code,
        f'Antigravity configuration at {source.path} {message}',
        correction,
    )


def _handler_validation_finding(
    source: ConfigSource,
    handler: object,
) -> Finding | None:
    """Return the first structural problem in one handler."""
    if not isinstance(handler, dict):
        return _finding(source, 'agy_hook_malformed', 'contains a malformed hook handler')
    handler_data = cast('dict[str, object]', handler)
    handler_type = handler_data.get('type', 'command')
    if handler_type != 'command':
        return _finding(
            source,
            'agy_hook_type_unsupported',
            'contains a hook whose type is not the supported command form',
        )
    if not isinstance(handler_data.get('command'), str) or not handler_data.get('command'):
        return _finding(
            source,
            'agy_command_malformed',
            'contains a command hook without a non-empty string command',
        )
    if 'args' in handler_data and (
        not isinstance(raw_args := handler_data['args'], list)
        or not all(isinstance(argument, str) for argument in cast('list[object]', raw_args))
    ):
        return _finding(
            source,
            'agy_args_malformed',
            'contains a command hook with malformed args',
        )
    if 'timeout' in handler_data and parse_timeout(handler_data['timeout'], positive=True) is None:
        return _finding(
            source,
            'agy_timeout_malformed',
            'contains a command hook with a malformed timeout',
        )
    return None


def _inspection_limitations() -> tuple[str, ...]:
    """Return Antigravity sources static file discovery cannot inspect."""
    return _LIMITATIONS


def _load_source(source: ConfigSource) -> tuple[dict[str, object] | None, Finding | None]:
    """Load one strict JSON source without exposing its content."""
    try:
        text = source.path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None, _finding(
            source,
            'agy_source_encoding_malformed',
            'contains malformed UTF-8',
        )
    except OSError:
        return None, _finding(source, 'agy_source_unreadable', 'cannot be read')
    try:
        loaded = load_object_text(text)
    except JsonObjectExpectedError:
        return None, _finding(source, 'agy_settings_malformed', 'must contain a JSON object')
    except JsonProtocolError:
        return None, _finding(source, 'agy_json_malformed', 'contains malformed JSON')
    return cast('dict[str, object]', loaded), None


def _registration(
    source: ConfigSource,
    event: str,
    matcher: str | None,
    handler: object,
    *,
    enabled: bool | None,
) -> tuple[Registration | None, list[Finding]]:
    """Parse one command handler."""
    validation_finding = _handler_validation_finding(source, handler)
    if validation_finding is not None:
        return None, [validation_finding]
    handler_data = cast('dict[str, object]', handler)
    raw_args = cast('list[str] | None', handler_data.get('args'))
    raw_timeout = handler_data.get('timeout')
    timeout = _DEFAULT_HANDLER_TIMEOUT
    if raw_timeout is not None:
        parsed_timeout = parse_timeout(raw_timeout, positive=True)
        if parsed_timeout is None:
            raise AssertionError('validated handler timeout is missing')
        timeout = parsed_timeout
    findings: list[Finding] = []
    if raw_timeout is None:
        findings.append(
            Finding(
                FindingSeverity.INFO,
                'agy_timeout_default_documented',
                'As documented on 2026-07-28, Antigravity defaults command handler '
                f'timeouts to {_DEFAULT_HANDLER_TIMEOUT:g} seconds',
            )
        )
    if event not in _NATIVE_EVENTS:
        findings.append(
            Finding(
                FindingSeverity.WARNING,
                'agy_native_event_unknown',
                'Antigravity native event is not a documented Antigravity hook event',
                'Use a documented Antigravity hook event name.',
            )
        )
    registration = Registration(
        source=source,
        event=event,
        matcher=matcher,
        command=cast('str', handler_data['command']),
        args=tuple(raw_args) if isinstance(raw_args, list) else None,
        launch_semantics=LaunchSemantics.POSIX_SHELL,
        command_windows=None,
        windows_launch_semantics=LaunchSemantics.WINDOWS_SHELL,
        timeout=timeout,
        enabled=enabled,
        type=cast('str', handler_data.get('type', 'command')),
        configuration_findings=tuple(findings),
    )
    return registration, []


def _event_registrations(
    source: ConfigSource,
    event: object,
    groups: object,
    *,
    enabled: bool | None,
) -> tuple[list[Registration], list[Finding]]:
    """Parse one event layer."""
    if not isinstance(event, str) or not isinstance(groups, list):
        return [], [_finding(source, 'agy_event_malformed', 'contains a malformed event')]
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for group in cast('list[object]', groups):
        if not isinstance(group, dict):
            findings.append(
                _finding(
                    source,
                    'agy_matcher_group_malformed',
                    'contains a malformed matcher group',
                )
            )
            continue
        group_data = cast('dict[str, object]', group)
        matcher = group_data.get('matcher')
        if 'matcher' in group_data and not isinstance(matcher, str):
            findings.append(
                _finding(source, 'agy_matcher_malformed', 'contains a malformed matcher')
            )
            continue
        handlers = group_data.get('hooks')
        if not isinstance(handlers, list):
            findings.append(
                _finding(
                    source,
                    'agy_handler_list_malformed',
                    'contains a malformed hook handler list',
                )
            )
            continue
        for handler in cast('list[object]', handlers):
            registration, handler_findings = _registration(
                source,
                event,
                matcher if isinstance(matcher, str) else None,
                handler,
                enabled=enabled,
            )
            if registration is not None:
                registrations.append(registration)
            findings.extend(handler_findings)
    return registrations, findings


def _named_registrations(
    source: ConfigSource,
    loaded: dict[str, object],
) -> tuple[list[Registration], list[Finding]]:
    """Parse the documented named-hook map."""
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for named_hook in loaded.values():
        if not isinstance(named_hook, dict):
            findings.append(
                _finding(
                    source,
                    'agy_named_hook_malformed',
                    'contains a malformed named hook',
                )
            )
            continue
        hook_data = cast('dict[str, object]', named_hook)
        enabled = hook_data.get('enabled', True)
        if not isinstance(enabled, bool):
            findings.append(
                _finding(
                    source,
                    'agy_enabled_malformed',
                    'contains a named hook with malformed enabled state',
                )
            )
            continue
        for event, groups in hook_data.items():
            if event == 'enabled':
                continue
            event_registrations, event_findings = _event_registrations(
                source,
                event,
                groups,
                enabled=enabled,
            )
            registrations.extend(event_registrations)
            findings.extend(event_findings)
    return registrations, findings


def _load_registrations(
    sources: Iterable[ConfigSource],
) -> tuple[tuple[Registration, ...], tuple[Finding, ...]]:
    """Extract registrations and findings in source order."""
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for source in sources:
        if not source.exists:
            continue
        loaded, finding = _load_source(source)
        if finding is not None:
            findings.append(finding)
            continue
        if loaded is None:
            continue
        source_registrations, source_findings = _named_registrations(source, loaded)
        registrations.extend(source_registrations)
        findings.extend(source_findings)
    return tuple(registrations), tuple(findings)
