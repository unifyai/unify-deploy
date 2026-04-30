"""Load scenario specs from integration packages or standalone YAML files.

Scenario discovery mirrors the integration roots: generic ``packages/`` and
private ``client_packages/`` are searched by default, and opt-in
``mock_packages/`` is only searched when the caller explicitly asks for it.
This keeps deploy-time resolution hermetic while still letting scenario E2Es
opt into mock-rooted scenarios through ``include_mock_packages=True``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from unity_deploy.customization.integrations.discovery import (
    _BUILTIN_DIR,
    _CLIENT_DIR,
    _MOCK_DIR,
)
from unity_deploy.customization.scenarios.types import ScenarioSpec

_SCENARIOS_DIR = "scenarios"


def load_scenario(path: Path | str) -> ScenarioSpec:
    """Load a single scenario YAML file."""
    scenario_path = Path(path)
    raw = yaml.safe_load(scenario_path.read_text()) or {}
    return ScenarioSpec.model_validate(raw)


def load_scenarios(root: Path | str) -> list[ScenarioSpec]:
    """Load every scenario YAML file under an integration package root."""
    scenarios_dir = Path(root) / _SCENARIOS_DIR
    if not scenarios_dir.is_dir():
        return []

    specs: list[ScenarioSpec] = []
    for file in sorted([*scenarios_dir.glob("*.yaml"), *scenarios_dir.glob("*.yml")]):
        specs.append(load_scenario(file))
    return specs


def find_integration_root(
    slug: str,
    *,
    include_mock_packages: bool = False,
    search_paths: list[Path] | None = None,
) -> Path | None:
    """Return the first integration package root matching *slug*.

    The default search order is generic ``packages/`` then private
    ``client_packages/``. Mock packages are only consulted when
    ``include_mock_packages=True``.
    """
    paths = list(search_paths or [_BUILTIN_DIR, _CLIENT_DIR])
    if include_mock_packages and _MOCK_DIR not in paths:
        paths.append(_MOCK_DIR)

    for base in paths:
        candidate = base / slug
        if (candidate / "manifest.yaml").is_file():
            return candidate
    return None


def load_scenarios_from_integration(
    slug: str,
    *,
    include_mock_packages: bool = False,
    search_paths: list[Path] | None = None,
) -> list[ScenarioSpec]:
    """Load scenario specs bundled with an integration package.

    Mock-rooted scenarios are only returned when ``include_mock_packages``
    is true, matching deploy-time activation behavior.
    """
    root = find_integration_root(
        slug,
        include_mock_packages=include_mock_packages,
        search_paths=search_paths,
    )
    if root is None:
        return []
    return load_scenarios(root)
