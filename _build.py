"""Collect bundled agent entry points for distribution metadata."""

import tomllib
from pathlib import Path
from typing import override

from hatchling.metadata.plugin.interface import MetadataHookInterface


class PluginMetadataHook(MetadataHookInterface):
    """Include each bundled plugin's registration in the containing distribution."""

    @override
    def update(self, metadata: dict[str, object]) -> None:
        metadata['entry-points'] = _plugin_entry_points(Path(self.root))


def _plugin_entry_points(root: Path) -> dict[str, dict[str, str]]:
    """Read entry points declared by the root's agent plugin directories."""
    groups: dict[str, dict[str, str]] = {}
    for manifest in sorted((root / 'plugins').glob('*/adapter.toml')):
        with manifest.open('rb') as stream:
            entries = tomllib.load(stream)['entry-points']
        for group, targets in entries.items():
            registered = groups.setdefault(group, {})
            for name, target in targets.items():
                if name in registered:
                    raise ValueError(f'Duplicate entry point {group}:{name} in {manifest}')
                registered[name] = target
    return groups
