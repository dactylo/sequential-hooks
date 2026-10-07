from dataclasses import replace
from pathlib import Path

from sequential_hooks.hosts import ConfigSource, LaunchSemantics, Registration, SourceKind


def registration(
    command: str = 'sequential-hooks --agent codex child',
    **changes: object,
) -> Registration:
    if 'semantics' in changes:
        changes['launch_semantics'] = changes.pop('semantics')
    if 'windows_semantics' in changes:
        changes['windows_launch_semantics'] = changes.pop('windows_semantics')
    registration = Registration(
        source=ConfigSource(
            path=Path('/safe/config.json'),
            kind=SourceKind.USER,
            exists=True,
        ),
        event='PostToolUse',
        matcher=None,
        command=command,
        args=None,
        launch_semantics=LaunchSemantics.POSIX_SHELL,
        command_windows=None,
        windows_launch_semantics=None,
        timeout=30.0,
        enabled=True,
        type=None,
    )
    return replace(registration, **changes)
