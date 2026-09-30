import atexit
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from _build import _plugin_entry_points

BUILTIN_ENTRY_POINTS = _plugin_entry_points(Path(__file__).resolve().parent.parent)[
    'sequential_hooks.hosts'
]
_ENTRY_POINT_GROUP = 'sequential_hooks.hosts'
_SRC_ROOT = Path(__file__).resolve().parent.parent / 'src'
_TESTS_ROOT = Path(__file__).resolve().parent.parent


def register_distribution(root: Path, name: str, entry_points: Mapping[str, str]) -> None:
    # importlib.metadata derives the distribution name from the text before the first hyphen,
    # so the directory name must use underscores.
    dist = root / f'{name.replace("-", "_")}-0.0.0.dist-info'
    dist.mkdir()
    (dist / 'METADATA').write_text(
        f'Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.0\n', encoding='utf-8'
    )
    lines = [
        f'[{_ENTRY_POINT_GROUP}]',
        *(f'{host} = {target}' for host, target in entry_points.items()),
    ]
    (dist / 'entry_points.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def subprocess_environment(*extra_roots: Path) -> dict[str, str]:
    roots = [_SRC_ROOT, _TESTS_ROOT, _HOSTS, *extra_roots]
    return {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root) for root in roots)}


_HOSTS = Path(tempfile.mkdtemp(prefix='sequential-hooks-hosts-'))
atexit.register(shutil.rmtree, _HOSTS, ignore_errors=True)
register_distribution(_HOSTS, 'sequential-hooks', BUILTIN_ENTRY_POINTS)
