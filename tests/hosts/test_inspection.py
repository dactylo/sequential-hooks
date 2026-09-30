from pathlib import Path

import pytest

from sequential_hooks.hosts import parse_timeout, project_layers


@pytest.mark.parametrize(
    ('value', 'positive', 'expected'),
    [
        (True, False, None),
        (False, True, None),
        (None, False, None),
        ('1', True, None),
        (4, True, 4.0),
        (2.5, False, 2.5),
        (float('inf'), False, None),
        (float('-inf'), True, None),
        (float('nan'), False, None),
        (10**400, False, None),
        (0, True, None),
        (0, False, 0.0),
        (-3, True, None),
        (-3, False, -3.0),
    ],
)
def test_parse_timeout_applies_numeric_and_sign_boundaries(
    value: object,
    positive: bool,
    expected: float | None,
) -> None:
    assert parse_timeout(value, positive=positive) == expected


def test_project_layers_use_nearest_structural_root(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    cwd = root / 'one' / 'two'
    cwd.mkdir(parents=True)
    (root / '.git').mkdir()

    assert project_layers(cwd) == (root, root / 'one', cwd)
    assert project_layers(tmp_path) == (tmp_path,)
