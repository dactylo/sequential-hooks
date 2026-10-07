"""Register source-checkout distributions for core and bundled plugin tests.

Pytest loads this module to expose isolated bundled-host metadata to tests that
request it. Importing its distribution helper also creates temporary
bundled-host metadata for subprocesses, removed at normal process exit.
"""

import sys
from pathlib import Path

import pytest

from tests._distributions import BUILTIN_ENTRY_POINTS, register_distribution


@pytest.fixture
def distribution_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Register bundled hosts in isolated metadata prepended to `sys.path`.

    Args:
        tmp_path: Pytest temporary directory for this test.
        monkeypatch: Fixture that restores the original `sys.path` at teardown.

    Returns:
        Directory containing this test's distribution metadata, prepended to
        `sys.path` for the duration of the test.
    """
    root = tmp_path / 'distributions'
    root.mkdir()
    monkeypatch.setattr(sys, 'path', [str(root), *sys.path])
    register_distribution(root, 'sequential-hooks', BUILTIN_ENTRY_POINTS)
    return root
