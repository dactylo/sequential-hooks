import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imports.append(node.module)
    return imports


def test_core_does_not_import_bundled_plugins() -> None:
    for path in (_ROOT / 'src' / 'sequential_hooks').rglob('*.py'):
        assert not any(
            name == 'plugins' or name.startswith('plugins.') for name in _imports(path)
        ), path


def test_plugins_use_only_the_public_host_contract() -> None:
    for package in (_ROOT / 'plugins').glob('*/_sequential_hooks'):
        for path in package.rglob('*.py'):
            for name in _imports(path):
                if name == 'sequential_hooks' or name.startswith('sequential_hooks.'):
                    assert name == 'sequential_hooks.hosts', (path, name)
                if name == 'plugins' or name.startswith('plugins.'):
                    assert name.startswith(f'plugins.{package.parent.name}._sequential_hooks'), (
                        path,
                        name,
                    )
