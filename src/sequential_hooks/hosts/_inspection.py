"""Provide shared configuration inspection primitives for host plugins."""

import math
from collections.abc import Iterable
from pathlib import Path

from sequential_hooks.hosts._records import SourceKind


def deduplicate_source_paths(
    paths: Iterable[tuple[Path, SourceKind]],
) -> list[tuple[Path, SourceKind]]:
    """Drop later duplicates of one candidate path, keeping the first scope.

    Args:
        paths: Candidate files with scopes in emission order.

    Returns:
        The same order with only the first occurrence of each path.
    """
    seen: set[Path] = set()
    unique: list[tuple[Path, SourceKind]] = []
    for path, kind in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, kind))
    return unique


def parse_timeout(value: object, *, positive: bool) -> float | None:
    """Convert one finite numeric timeout under a caller-owned sign rule.

    Args:
        value: Candidate timeout value.
        positive: Whether the timeout must be greater than zero.

    Returns:
        The finite converted timeout, or `None` when the value is invalid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


def project_layers(cwd: Path) -> tuple[Path, ...]:
    """Return structural project layers without invoking Git.

    Args:
        cwd: Directory whose enclosing project layers are required.

    Returns:
        Project root through `cwd`, or only `cwd` when no Git root exists.
    """
    resolved = cwd.resolve()
    root = next(
        (
            candidate
            for candidate in (resolved, *resolved.parents)
            if (candidate / '.git').exists()
        ),
        None,
    )
    if root is None:
        return (resolved,)
    relative = resolved.relative_to(root)
    layers = [root]
    current = root
    for part in relative.parts:
        current /= part
        layers.append(current)
    return tuple(layers)
