import ast
import tomllib
from dataclasses import fields
from pathlib import Path

import sequential_hooks
from sequential_hooks import _types

_ROOT = Path(__file__).parents[1]
_RUNTIME_ROOT = _ROOT / 'src' / 'sequential_hooks'


def test_all_runtime_modules_parse_as_python_313() -> None:
    for path in sorted(_RUNTIME_ROOT.rglob('*.py')):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 13))


def test_makefile_type_recipe_uses_tracked_python_313_configuration() -> None:
    lines = (_ROOT / 'Makefile').read_text().splitlines()
    recipe_index = lines.index('type-check: env-check')
    recipes: list[str] = []
    for line in lines[recipe_index + 1 :]:
        if not line.startswith('\t'):
            break
        recipes.append(line)
    assert len(recipes) == 3
    for platform, recipe in zip(('Linux', 'Darwin', 'Windows'), recipes, strict=True):
        assert '$(DEV_PYTHON) -m pyright' in recipe
        assert '--project pyproject.toml' in recipe
        assert '--pythonpath "$(DEV_PYTHON)"' in recipe
        assert f'--pythonplatform {platform}' in recipe


def test_package_root_declares_no_public_python_api() -> None:
    assert sequential_hooks.__all__ == []


def test_project_declares_local_ci_tool_groups() -> None:
    config = tomllib.loads((_ROOT / 'pyproject.toml').read_text())

    assert 'dev' not in config['project'].get('optional-dependencies', {})
    assert config['dependency-groups']['dev'] == [
        'mdformat',
        'mdformat-config',
        'mdformat-frontmatter',
        'mdformat-gfm',
        'mdformat-pyproject',
        'mdformat-ruff',
        'mdformat-shfmt',
        'pyproject-fmt',
        'pyright',
        'ruff',
        'vulture',
        {'include-group': 'test'},
    ]
    assert config['dependency-groups']['test'] == ['pytest', 'pytest-xdist>=3.2']
    assert (_ROOT / 'requirements-ci.txt').is_file()


def test_project_declares_one_python_313_floor() -> None:
    config = tomllib.loads((_ROOT / 'pyproject.toml').read_text())
    classifiers = config['project']['classifiers']
    expected_classifiers = [
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
        'Programming Language :: Python :: 3.15',
    ]

    assert config['project']['requires-python'] == '>=3.13'
    assert [item for item in classifiers if item in expected_classifiers] == expected_classifiers
    assert config['tool']['ruff']['target-version'] == 'py313'
    assert config['tool']['pyright']['pythonVersion'] == '3.13'
    assert config['project'].get('dependencies', []) == []


def test_shared_failure_and_containment_values_are_stable() -> None:
    assert [(item.name, item.value) for item in _types.FailureKind] == [
        ('NOT_FOUND', 'not_found'),
        ('SPAWN', 'spawn'),
        ('CRASH', 'crash'),
        ('TIMEOUT', 'timeout'),
        ('OUTPUT_LIMIT', 'output_limit'),
        ('OUTPUT_WORKER_TIMEOUT', 'output_worker_timeout'),
        ('CONTAINMENT', 'containment'),
        ('UNSUPPORTED_EXECUTABLE', 'unsupported_executable'),
    ]
    assert hasattr(_types, 'ContainmentState')
    assert [(item.name, item.value) for item in _types.ContainmentState] == [
        ('CONTAINED', 'contained'),
        ('UNCONTAINED', 'uncontained'),
    ]


def test_step_result_appends_containment_default() -> None:
    assert [field.name for field in fields(_types.StepResult)] == [
        'returncode',
        'stdout',
        'blocking_stderr',
        'failure',
        'containment',
    ]
    result = _types.StepResult(0, b'', '')
    assert hasattr(result, 'containment')
    assert result.containment is _types.ContainmentState.CONTAINED
