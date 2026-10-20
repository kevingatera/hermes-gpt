"""Find local imports, including package initializers and lazy imports."""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / 'src'


def module_file(name: str) -> Path | None:
    path = SOURCE_ROOT.joinpath(*name.split('.'))
    for candidate in (path.with_suffix('.py'), path / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


def import_dependencies(entry: str) -> set[Path]:
    pending = [entry]
    visited: set[str] = set()
    files: set[Path] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        path = module_file(name)
        if path is None:
            continue
        files.add(path)
        parts = name.split('.')
        pending.extend('.'.join(parts[:depth]) for depth in range(1, len(parts)))
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                pending.append(node.module)
                pending.extend(f'{node.module}.{alias.name}' for alias in node.names)
    return files
