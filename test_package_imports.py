"""Ensure server imports remain available outside an editable checkout."""

import ast
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent


def test_server_local_import_dependencies_are_packaged():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        modules = set(tomllib.load(handle)["tool"]["setuptools"]["py-modules"])
    pending = ["server"]
    visited = set()
    missing = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if name not in modules:
            missing.add(name)
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        # Include lazy imports: optional tools must work when enabled too.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            pending.extend(name for name in names if (ROOT / f"{name}.py").is_file())
    assert not missing, f"server dependencies missing from py-modules: {sorted(missing)}"
