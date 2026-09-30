"""Inspect Claude Code hook configuration sources."""

import stat
import sys
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
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

_DisableSetting = tuple[bool | None, Finding | None, bool]

_LIMITATIONS = (
    'server-managed Claude Code settings cannot be inspected as a local file',
    'macOS com.anthropic.claudecode managed preferences or plist delivery cannot be inspected',
    (
        'Windows HKLM and HKCU SOFTWARE\\Policies\\ClaudeCode Settings registry delivery '
        'cannot be inspected'
    ),
    'plugin-provided Claude Code hooks cannot be inspected without scanning plugin caches',
)


class ClaudeDoctorLoader:
    """Inspect the Claude Code settings files through the published `DoctorLoader` contract."""

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


@dataclass(frozen=True)
class _Activation:
    """Describe activation established from inspected settings files."""

    _managed: bool | None
    _non_managed: bool | None


def _activation(
    sources: Collection[ConfigSource],
    settings: Mapping[Path, _DisableSetting],
) -> _Activation:
    """Resolve inspected-file activation without inventing same-scope order."""
    by_scope = {
        scope: [
            value
            for source in sources
            if source.kind is scope
            for value, _finding_value, rejected in (settings[source.path],)
            if value is not None and not rejected
        ]
        for scope in (
            SourceKind.MANAGED,
            SourceKind.LOCAL,
            SourceKind.PROJECT,
            SourceKind.USER,
        )
    }
    winning_scope = next((scope for scope, values in by_scope.items() if values), None)
    if winning_scope is None:
        return _Activation(_managed=True, _non_managed=True)
    values = by_scope[winning_scope]
    if winning_scope is SourceKind.MANAGED:
        disabled = values[-1]
    elif len(set(values)) == 1:
        disabled = values[0]
    else:
        return _Activation(_managed=True, _non_managed=None)
    if not disabled:
        return _Activation(_managed=True, _non_managed=True)
    if winning_scope is SourceKind.MANAGED:
        return _Activation(_managed=False, _non_managed=False)
    return _Activation(_managed=True, _non_managed=False)


def _enabled(source: ConfigSource, activation: _Activation) -> bool | None:
    """Return the inspected activation state for one registration source."""
    if source.kind is SourceKind.MANAGED:
        return activation._managed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    return activation._non_managed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


def _finding(source: ConfigSource, code: str, message: str) -> Finding:
    """Build a source-specific structural finding."""
    return Finding(
        FindingSeverity.WARNING,
        code,
        f'Claude configuration at {source.path} {message}',
    )


def _disable_setting(
    source: ConfigSource,
    loaded: dict[str, object] | None,
) -> _DisableSetting:
    """Return the visible setting, finding, and whole-source rejection state."""
    if loaded is None or 'disableAllHooks' not in loaded:
        return None, None, False
    value = loaded['disableAllHooks']
    if isinstance(value, bool):
        return value, None, False
    finding = _finding(
        source,
        'claude_disable_all_hooks_malformed',
        'contains a malformed disableAllHooks setting',
    )
    return None, finding, source.kind is not SourceKind.MANAGED


def _hook_validation_finding(
    source: ConfigSource,
    hook: object,
) -> Finding | None:
    """Return the first structural problem in one hook."""
    finding = None
    if not isinstance(hook, dict):
        finding = _finding(
            source,
            'claude_hook_malformed',
            'contains a malformed hook',
        )
    else:
        hook_data = cast('dict[str, object]', hook)
        if hook_data.get('type') != 'command':
            finding = _finding(
                source,
                'claude_hook_type_unsupported',
                'contains a hook whose type is not the documented command form',
            )
        elif not isinstance(hook_data.get('command'), str) or not hook_data.get('command'):
            finding = _finding(
                source,
                'claude_command_malformed',
                'contains a command hook without a string command',
            )
        elif 'args' in hook_data and (
            not isinstance(raw_args := hook_data['args'], list)
            or not all(isinstance(argument, str) for argument in cast('list[object]', raw_args))
        ):
            finding = _finding(
                source,
                'claude_args_malformed',
                'contains a command hook with malformed args',
            )
        elif (
            'timeout' in hook_data
            and parse_timeout(
                hook_data['timeout'],
                positive=False,
            )
            is None
        ):
            finding = _finding(
                source,
                'claude_timeout_malformed',
                'contains a command hook with a malformed timeout',
            )
    return finding


def _inspection_limitations() -> tuple[str, ...]:
    """Return Claude sources static inspection cannot establish."""
    return _LIMITATIONS


def _load_source(source: ConfigSource) -> tuple[dict[str, object] | None, Finding | None]:
    """Load one source without exposing content in diagnostics."""
    try:
        text = source.path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None, _finding(
            source,
            'claude_source_encoding_malformed',
            'contains malformed UTF-8',
        )
    except OSError:
        return None, _finding(source, 'claude_source_unreadable', 'cannot be read')
    try:
        loaded = load_object_text(text)
    except JsonObjectExpectedError:
        return None, _finding(source, 'claude_settings_malformed', 'must contain a JSON object')
    except JsonProtocolError:
        return None, _finding(source, 'claude_json_malformed', 'contains malformed JSON')
    return cast('dict[str, object]', loaded), None


def _managed_directory(system_root: Path) -> Path:
    """Return the documented managed-settings directory for this platform."""
    if sys.platform == 'darwin':
        return system_root / 'Library/Application Support/ClaudeCode'
    if sys.platform == 'win32':
        return system_root / 'Program Files/ClaudeCode'
    return system_root / 'etc/claude-code'


def _managed_drop_ins(directory: Path) -> tuple[ConfigSource, ...]:
    """Return documented managed drop-ins or isolate enumeration failure."""
    try:
        if not stat.S_ISDIR(directory.stat().st_mode):
            return ()
        paths = tuple(directory.iterdir())
    except FileNotFoundError:
        return ()
    except OSError:
        return (ConfigSource(directory, SourceKind.MANAGED, exists=True),)
    return tuple(
        ConfigSource(path, SourceKind.MANAGED, exists=True)
        for path in sorted(paths, key=lambda item: item.name)
        if path.is_file() and not path.name.startswith('.') and path.suffix == '.json'
    )


def _discover_sources(
    home: Path,
    cwd: Path,
    system_root: Path,
) -> tuple[ConfigSource, ...]:
    """Return Claude sources in deterministic inspection order."""
    paths: list[tuple[Path, SourceKind]] = [(home / '.claude/settings.json', SourceKind.USER)]
    for layer in project_layers(cwd):
        paths.extend(
            (
                (layer / '.claude/settings.json', SourceKind.PROJECT),
                (layer / '.claude/settings.local.json', SourceKind.LOCAL),
            )
        )
    managed_directory = _managed_directory(system_root)
    paths.append((managed_directory / 'managed-settings.json', SourceKind.MANAGED))
    drop_in_directory = managed_directory / 'managed-settings.d'
    sources = tuple(
        ConfigSource(path, kind, path.exists()) for path, kind in deduplicate_source_paths(paths)
    )
    return (*sources, *_managed_drop_ins(drop_in_directory))


def _registration(
    source: ConfigSource,
    event: str,
    matcher: str | None,
    hook: object,
    *,
    enabled: bool | None,
) -> tuple[Registration | None, Finding | None]:
    """Parse one command hook."""
    validation_finding = _hook_validation_finding(source, hook)
    if validation_finding is not None:
        return None, validation_finding
    hook_data = cast('dict[str, object]', hook)
    command = cast('str', hook_data.get('command'))
    raw_args = cast('list[str] | None', hook_data.get('args'))
    raw_timeout = hook_data.get('timeout')

    args = tuple(raw_args) if isinstance(raw_args, list) else None
    direct = args is not None
    registration = Registration(
        source=source,
        event=event,
        matcher=matcher,
        command=command,
        args=args,
        launch_semantics=(LaunchSemantics.DIRECT_ARGV if direct else LaunchSemantics.POSIX_SHELL),
        command_windows=None,
        windows_launch_semantics=(
            LaunchSemantics.DIRECT_ARGV if direct else LaunchSemantics.HOST_TOKENIZER
        ),
        timeout=parse_timeout(raw_timeout, positive=False),
        enabled=enabled,
        type=cast('str', hook_data['type']),
    )
    if direct:
        return registration, None
    if sys.platform != 'win32':
        finding = Finding(
            FindingSeverity.INFO,
            'claude_exec_form_recommended',
            'Claude Code command strings use a POSIX shell',
            'Use the exec form with explicit args.',
        )
    else:
        finding = Finding(
            FindingSeverity.INFO,
            'claude_windows_shell_conditional',
            'Claude Code command strings use Git Bash on Windows when statically '
            'established as available, otherwise PowerShell; shell availability was not probed',
            'Use the exec form with explicit args, especially on Windows.',
        )

    return replace(registration, configuration_findings=(finding,)), None


def _event_registrations(
    source: ConfigSource,
    event: object,
    groups: object,
    *,
    enabled: bool | None,
) -> tuple[list[Registration], list[Finding]]:
    """Parse one event layer."""
    if not isinstance(event, str) or not isinstance(groups, list):
        return [], [_finding(source, 'claude_event_malformed', 'contains a malformed event')]
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for group in cast('list[object]', groups):
        if not isinstance(group, dict):
            findings.append(
                _finding(
                    source, 'claude_matcher_group_malformed', 'contains a malformed matcher group'
                )
            )
            continue
        group_data = cast('dict[str, object]', group)
        matcher = group_data.get('matcher')
        if 'matcher' in group_data and not isinstance(matcher, str):
            findings.append(
                _finding(source, 'claude_matcher_malformed', 'contains a malformed matcher')
            )
            continue
        parsed_matcher = cast('str | None', matcher)
        hooks = group_data.get('hooks')
        if not isinstance(hooks, list):
            findings.append(
                _finding(source, 'claude_hooks_malformed', 'contains a malformed hooks list')
            )
            continue
        for hook in cast('list[object]', hooks):
            registration, finding = _registration(
                source,
                event,
                parsed_matcher,
                hook,
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
    source_list = tuple(sources)
    loaded_sources = tuple(
        (source, *_load_source(source)) if source.exists else (source, None, None)
        for source in source_list
    )
    settings = {
        source.path: _disable_setting(source, loaded)
        for source, loaded, _finding_value in loaded_sources
    }
    activation = _activation(source_list, settings)
    registrations: list[Registration] = []
    findings: list[Finding] = []
    for source, loaded, load_finding in loaded_sources:
        if load_finding is not None:
            findings.append(load_finding)
            continue
        _value, setting_finding, rejected = settings[source.path]
        if setting_finding is not None:
            findings.append(setting_finding)
        if loaded is None or rejected:
            continue
        hooks = loaded.get('hooks')
        if hooks is None:
            continue
        if not isinstance(hooks, dict):
            findings.append(
                _finding(source, 'claude_hooks_malformed', 'contains a malformed hooks object')
            )
            continue
        hook_events = cast('dict[str, object]', hooks)
        for event, groups in hook_events.items():
            event_registrations, event_findings = _event_registrations(
                source,
                event,
                groups,
                enabled=_enabled(source, activation),
            )
            registrations.extend(event_registrations)
            findings.extend(event_findings)
    return tuple(registrations), tuple(findings)
