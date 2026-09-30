"""Resolve supported child executable paths without launching them."""

import os
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_DEFAULT_WINDOWS_PATHEXT = ('.COM', '.EXE', '.BAT', '.CMD')
_RUNNABLE_WINDOWS_SUFFIXES = frozenset({'.com', '.exe'})
_UNSUPPORTED_WINDOWS_SUFFIXES = frozenset({'.bat', '.cmd', '.ps1'})
_WINDOWS_PATH_SEPARATOR = ';'


class ExecutableStatus(StrEnum):
    """Classify a resolved child executable.

    Attributes:
        RUNNABLE: The executable can be started directly.
        NOT_FOUND: No matching executable could be resolved.
        UNSUPPORTED_SHIM: Resolution found a shell-dependent Windows shim.
    """

    RUNNABLE = 'runnable'
    NOT_FOUND = 'not_found'
    UNSUPPORTED_SHIM = 'unsupported_shim'


@dataclass(frozen=True)
class ExecutableResolution:
    """Describe the static resolution of one executable token.

    Args:
        requested: Literal executable token supplied by the caller.
        resolved: Matching filesystem path, if one was found.
        status: Classification of the resolution result.
        correction: Safe corrective guidance for an unsupported result.

    Attributes:
        requested: Literal executable token supplied by the caller.
        resolved: Matching filesystem path, if one was found.
        status: Classification of the resolution result.
        correction: Safe corrective guidance for an unsupported result.
    """

    requested: str
    resolved: Path | None
    status: ExecutableStatus
    correction: str | None = None


def _case_insensitive_file(candidate: Path) -> Path | None:
    """Return the actual-cased file matching one Windows candidate."""
    current = Path(candidate.anchor) if candidate.is_absolute() else Path()
    parts = candidate.parts[1:] if candidate.is_absolute() else candidate.parts
    for part in parts:
        if part == '..':
            current /= part
            continue
        try:
            matches = sorted(
                (child for child in current.iterdir() if child.name.casefold() == part.casefold()),
                key=lambda child: child.name,
            )
        except OSError:
            return None
        if not matches:
            return None
        current = next((child for child in matches if child.name == part), matches[0])
    return current if current.is_file() else None


def _not_found(requested: str) -> ExecutableResolution:
    """Build a missing-executable result."""
    return ExecutableResolution(requested, None, ExecutableStatus.NOT_FOUND)


def _resolve_posix(executable: str, path_value: str | None) -> ExecutableResolution:
    """Resolve one executable with POSIX path and permission rules."""
    if '/' in executable:
        candidate = Path(executable)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return ExecutableResolution(executable, candidate, ExecutableStatus.RUNNABLE)
        return _not_found(executable)
    if path_value is None:
        return _not_found(executable)
    resolved = shutil.which(executable, path=path_value)
    if resolved is None:
        return _not_found(executable)
    return ExecutableResolution(executable, Path(resolved), ExecutableStatus.RUNNABLE)


def _windows_candidate_names(executable: str, pathext: tuple[str, ...]) -> tuple[str, ...]:
    """Apply Windows executable suffix search rules to one token."""
    suffix = Path(executable).suffix.casefold()
    if (
        suffix in _RUNNABLE_WINDOWS_SUFFIXES
        or suffix in _UNSUPPORTED_WINDOWS_SUFFIXES
        or any(suffix == extension.casefold() for extension in pathext)
    ):
        return (executable,)
    return tuple(f'{executable}{extension}' for extension in pathext)


def _windows_resolution(
    requested: str,
    candidate: Path,
) -> ExecutableResolution | None:
    """Classify one existing native or simulated Windows candidate."""
    if sys.platform == 'win32':
        resolved = candidate if candidate.is_file() else None
    else:
        resolved = _case_insensitive_file(candidate)
    if resolved is None:
        return None
    if resolved.suffix.casefold() in _UNSUPPORTED_WINDOWS_SUFFIXES:
        correction = (
            f'Use a real executable entry point for {requested}; '
            f'resolved unsupported Windows shim {resolved}.'
        )
        return ExecutableResolution(
            requested,
            resolved,
            ExecutableStatus.UNSUPPORTED_SHIM,
            correction,
        )
    if resolved.suffix.casefold() not in _RUNNABLE_WINDOWS_SUFFIXES:
        return None
    return ExecutableResolution(requested, resolved, ExecutableStatus.RUNNABLE)


def _resolve_windows(
    executable: str,
    path_value: str | None,
    pathext_value: str | None,
) -> ExecutableResolution:
    """Resolve one executable with Windows search rules."""
    pathext = (
        tuple(extension for extension in pathext_value.split(_WINDOWS_PATH_SEPARATOR) if extension)
        if pathext_value is not None
        else ()
    )
    if not pathext:
        pathext = _DEFAULT_WINDOWS_PATHEXT
    names = _windows_candidate_names(executable.replace('\\', os.sep), pathext)
    if '/' in executable or '\\' in executable:
        directories = ('',)
    elif path_value:
        directories = tuple(path_value.split(_WINDOWS_PATH_SEPARATOR))
    else:
        directories = ()

    for directory in directories:
        normalized_directory = directory.replace('\\', os.sep)
        if len(directory) > 1 and directory.startswith('"') and directory.endswith('"'):
            normalized_directory = normalized_directory[1:-1]
        for name in names:
            candidate = Path(normalized_directory) / name
            if resolution := _windows_resolution(executable, candidate):
                return resolution
    return _not_found(executable)


def resolve_executable(
    executable: str,
    *,
    path_value: str | None,
    windows: bool,
    pathext_value: str | None,
) -> ExecutableResolution:
    """Resolve one literal executable without starting it.

    Args:
        executable: Literal executable token to resolve.
        path_value: Injected executable search path, if set.
        windows: Whether to apply Windows lookup rules.
        pathext_value: Injected Windows executable suffix list, if set.

    Returns:
        Static executable resolution and any required correction.
    """
    if not executable:
        return _not_found(executable)
    if windows:
        return _resolve_windows(executable, path_value, pathext_value)
    return _resolve_posix(executable, path_value)
