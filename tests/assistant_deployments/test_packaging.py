from __future__ import annotations

import tomllib
from pathlib import Path


def test_deployment_python_assets_are_included_as_package_data():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    package_data = pyproject["tool"]["setuptools"]["package-data"]["unify_deploy"]

    assert "**/*.py" in package_data
