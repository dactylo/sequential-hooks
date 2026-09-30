"""Inspect Codex hook configuration sources."""

import sys
import tomllib
from collections.abc import Collection, Iterable, Iterator
from dataclasses import replace
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
    deduplicate_source_paths,
    load_object_text,
    parse_timeout,
    project_layers,
)

_LoadedSource = tuple[ConfigSource, dict[str, object] | None, Finding | None]

_LIMITATIONS = (
    'active profiles cannot be established from the inspected configuration files',
    'session overrides cannot be established from the inspected configuration files',
    'plugin hooks cannot be inspected without scanning enabled plugin installations',
    'persisted hook trust state cannot be established from hook definitions',
    'cloud or MDM managed hooks cannot be inspected as ordinary local configuration',
    'managed requirements.toml hooks and constraints cannot be established here',
    'allow_managed_hooks_only can suppress user, project, session, and plugin hooks',
    'local source absence does not prove the absence of an effective registration',
)


class CodexDoctorLoader:
    """Inspect the Codex hooks and config files through the published `DoctorLoader` contract."""

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
        del registration, inspected, windows
        return ()

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


def _discover_sources(
    home: Path,
    cwd: Path,
    system_root: Path,
) -> tuple[ConfigSource, ...]:
    """Return Codex sources in deterministic inspection order."""
    paths: list[tuple[Path, SourceKind]] = [
        (home / '.codex/hooks.json', SourceKind.USER),
        (home / '.codex/config.toml', SourceKind.USER),
        (system_root / 'etc/codex/hooks.json', SourceKind.SYSTEM),
        (system_root / 'etc/codex/config.toml', SourceKind.SYSTEM),
    ]
    for layer in project_layers(cwd):
        paths.extend(
            (
                (layer / '.codex/hooks.json', SourceKind.PROJECT),
                (layer / '.codex/config.toml', SourceKind.PROJECT),
            )
        )
    return tuple(
        ConfigSource(path, kind, path.exists()) for path, kind in deduplicate_source_paths(paths)
    )


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
        f'Codex configuration at {source.path} {message}',
        correction,
    )


def _activation_finding(source: ConfigSource) -> Finding:
    """Describe source activation without taking ownership from Codex."""
    if source.kind is SourceKind.SYSTEM:
        return Finding(
            FindingSeverity.INFO,
            'codex_system_policy_source',
            f'Codex configuration at {source.path} is a managed policy source; '
            'Codex determines activation',
        )
    if source.kind is SourceKind.PROJECT:
        return _finding(
            source,
            'codex_project_trust_unknown',
            'has hook definitions whose project trust state cannot be established statically',
            'Review the hook with /hooks inside Codex.',
        )
    return _finding(
        source,
        'codex_hook_trust_unknown',
        'has a non-managed hook definition whose trust state cannot be established statically',
        'Review the hook with /hooks inside Codex.',
    )


def _feature_state(
    source: ConfigSource,
    loaded: dict[str, object],
) -> tuple[bool | None, list[Finding]]:
    """Return this TOML source's hook feature state and compatibility findings."""
    features = loaded.get('features')
    if features is None:
        return True, []
    findings: list[Finding] = []
    if not isinstance(features, dict):
        findings.append(_finding(source, 'codex_features_malformed', 'has malformed features'))
        return None, findings
    feature_data = cast('dict[str, object]', features)
    if 'codex_hooks' in feature_data:
        findings.append(
            _finding(
                source,
                'codex_feature_alias_deprecated',
                'uses the deprecated features.codex_hooks compatibility alias',
                'Use features.hooks instead.',
            )
        )
    canonical = feature_data.get('hooks')
    alias = feature_data.get('codex_hooks')
    if 'hooks' in feature_data:
        if isinstance(canonical, bool):
            enabled: bool | None = canonical
        else:
            enabled = None
            findings.append(
                _finding(
                    source,
                    'codex_feature_hooks_malformed',
                    'has a malformed features.hooks value',
                )
            )
    elif 'codex_hooks' in feature_data:
        if isinstance(alias, bool):
            enabled = alias
        else:
            enabled = None
            findings.append(
                _finding(
                    source,
                    'codex_feature_alias_malformed',
                    'has a malformed features.codex_hooks value',
                )
            )
    else:
        enabled = True
    return enabled, findings


def _handler_fields_finding(
    source: ConfigSource,
    handler_data: dict[str, object],
) -> Finding | None:
    """Return the first malformed portable command field."""
    if not isinstance(handler_data.get('command'), str) or not handler_data.get('command'):
        return _finding(
            source,
            'codex_command_malformed',
            'contains a command hook without a non-empty string command',
        )
    if 'args' in handler_data and (
        not isinstance(raw_args := handler_data['args'], list)
        or not all(isinstance(argument, str) for argument in cast('list[object]', raw_args))
    ):
        return _finding(
            source,
            'codex_args_malformed',
            'contains a command hook with malformed args',
        )
    if (
        'timeout' in handler_data
        and parse_timeout(
            handler_data['timeout'],
            positive=False,
        )
        is None
    ):
        return _finding(
            source,
            'codex_timeout_malformed',
            'contains a command hook with a malformed timeout',
        )
    if 'async' in handler_data and not isinstance(handler_data['async'], bool):
        return _finding(
            source,
            'codex_async_malformed',
            'contains a command hook with a malformed async value',
        )
    return None


def _inspection_limitations() -> tuple[str, ...]:
    """Return Codex sources static inspection cannot establish."""
    return _LIMITATIONS


def _is_posix_only_command(command: str) -> bool:
    """Return whether a command explicitly launches a POSIX shell."""
    stripped = command.lstrip()
    return stripped.startswith(('sh ', 'bash ', '/bin/sh ', '/bin/bash '))


def _iter_event_items(
    hooks: dict[str, object],
    *,
    toml: bool,
) -> Iterator[tuple[str, object]]:
    """Yield hook events without product-owned TOML state metadata."""
    for event, groups in hooks.items():
        if not toml or event != 'state':
            yield event, groups


def _layer_feature_states(
    loaded_sources: Collection[_LoadedSource],
) -> tuple[dict[Path, bool | None], dict[Path, tuple[Finding, ...]]]:
    """Collect the feature state established by each layer's TOML source."""
    states: dict[Path, bool | None] = {}
    findings: dict[Path, tuple[Finding, ...]] = {}
    for source, loaded, source_finding in loaded_sources:
        if source.path.suffix != '.toml' or not source.exists:
            continue
        if source_finding is not None or loaded is None:
            states[source.path.parent] = None
            continue
        state, feature_findings = _feature_state(source, loaded)
        states[source.path.parent] = state
        findings[source.path] = tuple(feature_findings)
    return states, findings


def _load_json_source(
    source: ConfigSource,
    text: str,
) -> tuple[dict[str, object] | None, Finding | None]:
    """Load one strict JSON hook source."""
    try:
        loaded = load_object_text(text)
    except JsonObjectExpectedError:
        return None, _finding(source, 'codex_settings_malformed', 'must contain a JSON object')
    except JsonProtocolError:
        return None, _finding(source, 'codex_json_malformed', 'contains malformed JSON')
    return cast('dict[str, object]', loaded), None


def _load_toml_source(
    source: ConfigSource,
    text: str,
) -> tuple[dict[str, object] | None, Finding | None]:
    """Load one TOML configuration source."""
    try:
        return tomllib.loads(text), None
    except (tomllib.TOMLDecodeError, ValueError):
        return None, _finding(source, 'codex_toml_malformed', 'contains malformed TOML')


def _load_source(source: ConfigSource) -> tuple[dict[str, object] | None, Finding | None]:
    """Load one source without exposing content in diagnostics."""
    try:
        text = source.path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None, _finding(
            source,
            'codex_source_encoding_malformed',
            'contains malformed UTF-8',
        )
    except OSError:
        return None, _finding(source, 'codex_source_unreadable', 'cannot be read')
    if source.path.suffix == '.toml':
        return _load_toml_source(source, text)
    return _load_json_source(source, text)


def _load_sources(sources: Iterable[ConfigSource]) -> tuple[_LoadedSource, ...]:
    """Load existing sources independently in inspection order."""
    loaded_sources: list[_LoadedSource] = []
    for source in sources:
        if source.exists:
            loaded, finding = _load_source(source)
        else:
            loaded, finding = None, None
        loaded_sources.append((source, loaded, finding))
    return tuple(loaded_sources)


def _windows_command_finding(
    source: ConfigSource,
    handler_data: dict[str, object],
) -> Finding | None:
    """Return a malformed or ambiguous Windows override finding."""
    toml = source.path.suffix == '.toml'
    windows_keys = ('commandWindows', 'command_windows') if toml else ('commandWindows',)
    if toml and all(key in handler_data for key in windows_keys):
        return _finding(
            source,
            'codex_windows_command_ambiguous',
            'contains both TOML spellings of the Windows command override',
        )
    if any(
        key in handler_data and (not isinstance(handler_data[key], str) or not handler_data[key])
        for key in windows_keys
    ):
        return _finding(
            source,
            'codex_windows_command_malformed',
            'contains a malformed Windows command override',
        )
    return None


def _handler_validation_finding(
    source: ConfigSource,
    handler: object,
) -> Finding | None:
    """Return the first structural problem in one handler."""
    if not isinstance(handler, dict):
        return _finding(source, 'codex_hook_malformed', 'contains a malformed hook handler')
    handler_data = cast('dict[str, object]', handler)
    if handler_data.get('type') != 'command':
        return _finding(
            source,
            'codex_hook_type_unsupported',
            'contains a hook whose type is not the supported command form',
        )
    fields_finding = _handler_fields_finding(source, handler_data)
    if fields_finding is not None:
        return fields_finding
    windows_finding = _windows_command_finding(source, handler_data)
    if windows_finding is not None:
        return windows_finding
    if handler_data.get('async') is True:
        return _finding(
            source,
            'codex_async_unsupported',
            'contains an asynchronous command hook that Codex does not run',
        )
    return None


def _windows_findings(
    source: ConfigSource,
    registrations: Collection[Registration],
) -> list[Finding]:
    """Diagnose explicit POSIX shell dependencies on native Windows."""
    if sys.platform != 'win32':
        return []
    return [
        _finding(
            source,
            'codex_windows_posix_command',
            'contains a command that explicitly depends on POSIX shell tokenization on Windows',
            'Add commandWindows and review the hook with /hooks inside Codex.',
        )
        for registration in registrations
        if registration.command_windows is None and _is_posix_only_command(registration.command)
    ]


def _registration(
    source: ConfigSource,
    event: str,
    matcher: str | None,
    handler: object,
    *,
    enabled: bool | None,
) -> tuple[Registration | None, Finding | None]:
    """Parse one supported synchronous command hook."""
    validation_finding = _handler_validation_finding(source, handler)
    if validation_finding is not None:
        return None, validation_finding
    handler_data = cast('dict[str, object]', handler)
    command = cast('str', handler_data['command'])
    raw_args = cast('list[str] | None', handler_data.get('args'))
    command_windows = handler_data.get('commandWindows')
    if source.path.suffix == '.toml' and command_windows is None:
        command_windows = handler_data.get('command_windows')
    parsed_windows = cast('str | None', command_windows)
    registration = Registration(
        source=source,
        event=event,
        matcher=matcher,
        command=command,
        args=tuple(raw_args) if isinstance(raw_args, list) else None,
        launch_semantics=LaunchSemantics.POSIX_SHELL,
        command_windows=parsed_windows,
        windows_launch_semantics=LaunchSemantics.WINDOWS_SHELL,
        timeout=parse_timeout(handler_data.get('timeout'), positive=False),
        enabled=enabled,
        type=cast('str', handler_data['type']),
    )
    return replace(
        registration,
        configuration_findings=tuple(_windows_findings(source, (registration,))),
    ), None


def _event_registrations(
    source: ConfigSource,
    event: object,
    groups: object,
    *,
    enabled: bool | None,
) -> tuple[list[Registration], list[Finding]]:
    """Parse one event layer."""
    if not isinstance(event, str) or not isinstance(groups, list):
        return [], [_finding(source, 'codex_event_malformed', 'contains a malformed event')]
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for group in cast('list[object]', groups):
        if not isinstance(group, dict):
            findings.append(
                _finding(
                    source,
                    'codex_matcher_group_malformed',
                    'contains a malformed matcher group',
                )
            )
            continue
        group_data = cast('dict[str, object]', group)
        matcher = group_data.get('matcher')
        if 'matcher' in group_data and not isinstance(matcher, str):
            findings.append(
                _finding(source, 'codex_matcher_malformed', 'contains a malformed matcher')
            )
            continue
        handlers = group_data.get('hooks')
        if not isinstance(handlers, list):
            findings.append(
                _finding(
                    source,
                    'codex_handler_list_malformed',
                    'contains a malformed hook handler list',
                )
            )
            continue
        for handler in cast('list[object]', handlers):
            registration, finding = _registration(
                source,
                event,
                cast('str | None', matcher),
                handler,
                enabled=enabled,
            )
            if registration is not None:
                registrations.append(registration)
            if finding is not None:
                findings.append(finding)
    return registrations, findings


def _load_registrations(
    sources: Iterable[ConfigSource],
) -> tuple[tuple[Registration, ...], tuple[Finding, ...]]:
    """Extract registrations and findings in source order."""
    registrations: list[Registration] = []
    findings: list[Finding] = []
    loaded_sources = _load_sources(sources)
    feature_states, feature_findings = _layer_feature_states(loaded_sources)
    for source, loaded, finding in loaded_sources:
        if not source.exists:
            continue
        if finding is not None:
            findings.append(finding)
            continue
        if loaded is None:
            continue
        toml = source.path.suffix == '.toml'
        enabled = feature_states.get(source.path.parent, True)
        if toml:
            findings.extend(feature_findings.get(source.path, ()))
        hooks = loaded.get('hooks')
        if hooks is None:
            continue
        if not isinstance(hooks, dict):
            findings.append(
                _finding(source, 'codex_hooks_malformed', 'contains a malformed hooks object')
            )
            continue
        source_registrations: list[Registration] = []
        hook_events = cast('dict[str, object]', hooks)
        for event, groups in _iter_event_items(hook_events, toml=toml):
            event_registrations, event_findings = _event_registrations(
                source,
                event,
                groups,
                enabled=enabled,
            )
            source_registrations.extend(event_registrations)
            findings.extend(event_findings)
        registrations.extend(source_registrations)
        if source_registrations:
            findings.append(_activation_finding(source))
    return tuple(registrations), tuple(findings)
