import configparser
import os
import shutil
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from _build import PluginMetadataHook
from tests._helpers import process_transcript, run_cli, run_setup

_ROOT = Path(__file__).resolve().parents[1]


def _build(project: Path, target: str) -> Path:
    run_setup(
        [sys.executable, '-m', 'hatchling', 'build', '-t', target],
        cwd=project,
    )
    suffix = '*.whl' if target == 'wheel' else '*.tar.gz'
    return next((project / 'dist').glob(suffix))


def _copy_project(destination: Path) -> None:
    destination.mkdir()
    for name in ('pyproject.toml', 'README.md', 'LICENSE', '_build.py'):
        source = _ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for name in ('src', 'plugins'):
        source = _ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                destination / name,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
            )


def _entry_points(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith('/entry_points.txt'))
        config = configparser.ConfigParser()
        config.read_string(archive.read(metadata).decode())
    return dict(config['sequential_hooks.hosts'])


def test_duplicate_plugin_registration_fails_the_metadata_build(tmp_path: Path) -> None:
    for directory in ('first', 'second'):
        plugin = tmp_path / 'plugins' / directory
        plugin.mkdir(parents=True)
        (plugin / 'adapter.toml').write_text(
            '[entry-points."sequential_hooks.hosts"]\nexample = "example:Host"\n',
            encoding='utf-8',
        )
    hook = PluginMetadataHook(str(tmp_path), {})
    with pytest.raises(ValueError, match=r'Duplicate entry point sequential_hooks\.hosts:example'):
        hook.update({})


def test_installed_wheel_reports_its_version(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    _copy_project(project)
    expected = tomllib.loads((project / 'pyproject.toml').read_text(encoding='utf-8'))['project'][
        'version'
    ]
    wheel = _build(project, 'wheel')
    installed = tmp_path / 'installed'
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            archive.extract(member, installed)

    result = run_cli(
        ['--version'],
        expect=0,
        cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(installed)},
    )
    assert result.stdout == f'sequential-hooks {expected}\n', process_transcript(result)


def test_plugin_directory_addition_survives_wheel_and_sdist(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    _copy_project(project)
    plugin = project / 'plugins' / 'example'
    package = plugin / '_sequential_hooks'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / '_adapter.py').write_text('HOST = "example"\n', encoding='utf-8')
    (plugin / 'adapter.toml').write_text(
        '[entry-points."sequential_hooks.hosts"]\n'
        'example = "plugins.example._sequential_hooks._adapter:HOST"\n',
        encoding='utf-8',
    )
    native_manifest = plugin / '.claude-plugin' / 'plugin.json'
    native_manifest.parent.mkdir()
    native_manifest.write_text('{}', encoding='utf-8')
    tests = plugin / 'tests'
    tests.mkdir()
    (tests / 'test_adapter.py').write_text('def test_example():\n    pass\n', encoding='utf-8')
    wheel = _build(project, 'wheel')
    expected_target = 'plugins.example._sequential_hooks._adapter:HOST'
    assert _entry_points(wheel)['example'] == expected_target
    with zipfile.ZipFile(wheel) as archive:
        assert 'plugins/example/_sequential_hooks/_adapter.py' in archive.namelist()
        assert 'plugins/example/tests/test_adapter.py' not in archive.namelist()
        assert 'plugins/example/adapter.toml' not in archive.namelist()
        assert 'plugins/example/.claude-plugin/plugin.json' not in archive.namelist()
        installed = tmp_path / 'installed'
        for name in archive.namelist():
            archive.extract(name, installed)
    run_setup(
        [
            sys.executable,
            '-c',
            (
                'from importlib.metadata import distribution; '
                'entries = distribution("sequential-hooks").entry_points; '
                'loaded = {ep.name: ep.load() for ep in entries '
                'if ep.group == "sequential_hooks.hosts"}; '
                'entry = next(ep for ep in entries '
                'if ep.group == "sequential_hooks.hosts" and ep.name == "example"); '
                'assert entry.load() == "example"'
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(installed)},
    )
    sdist = _build(project, 'sdist')
    source = tmp_path / 'source'
    with tarfile.open(sdist) as archive:
        archive.extractall(source, filter='data')
    unpacked = next(source.iterdir())
    assert (unpacked / 'plugins/example/adapter.toml').is_file()
    assert (unpacked / 'plugins/example/.claude-plugin/plugin.json').is_file()
    rebuilt = _build(unpacked, 'wheel')
    assert _entry_points(rebuilt) == _entry_points(wheel)
