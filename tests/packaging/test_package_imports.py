"""Check that server imports belong to declared packages."""

from pathlib import Path

from setuptools import find_packages

from tests.support.package_graph import SOURCE_ROOT, import_dependencies

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_server_local_import_dependencies_are_packaged():
    with (ROOT / 'pyproject.toml').open('rb') as handle:
        settings = tomllib.load(handle)['tool']['setuptools']
    assert settings['package-dir'] == {'': 'src'}
    discovery = settings['packages']['find']
    assert discovery['where'] == ['src']
    packages = set(find_packages(where=str(SOURCE_ROOT), include=discovery['include']))
    assert 'hermes_gpt' in packages
    missing = []
    for path in import_dependencies('hermes_gpt.server.app'):
        package = '.'.join(path.parent.relative_to(SOURCE_ROOT).parts)
        if package not in packages:
            missing.append(str(path.relative_to(SOURCE_ROOT)))
    assert not missing, f'Server dependencies outside declared packages: {missing}'
