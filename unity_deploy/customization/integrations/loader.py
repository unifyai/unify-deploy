"""Integration loading and aggregation.

Converts discovered integration manifests into structured data: typed
``Guidance`` and ``Secret`` models, function directories, MCP configs,
and URL mappings. Downstream consumers merge these into whatever runtime
configuration format they need.

The loader is root-agnostic: callers choose whether search paths include
generic ``packages/``, private ``client_packages/``, or opt-in
``mock_packages/`` roots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from unity.guidance_manager.types.guidance import Guidance
from unity_deploy.customization.integrations.discovery import (
    _load_manifest,
)
from unity_deploy.customization.integrations.types import (
    IntegrationManifest,
    MCPServerConfig,
)
from unity_deploy.customization.integrations.validation import validate_integration
from unity_deploy.customization.scenarios.loader import load_scenario
from unity_deploy.customization.scenarios.types import ScenarioSpec
from unity.secret_manager.types import Secret

logger = logging.getLogger(__name__)


@dataclass
class LoadedIntegration:
    """A single integration package loaded from disk."""

    manifest: IntegrationManifest
    root_dir: Path
    function_dir: Path | None = None
    venv_dir: Path | None = None
    guidance_entries: list[Guidance] = field(default_factory=list)
    secret_entries: list[Secret] = field(default_factory=list)
    scenario_specs: list[ScenarioSpec] = field(default_factory=list)
    url_mapping: dict[str, str] | None = None
    mcp_config: MCPServerConfig | None = None


@dataclass
class AggregatedIntegrations:
    """Merged data from multiple integrations, ready for downstream consumption."""

    function_dirs: list[Path] = field(default_factory=list)
    venv_dirs: list[Path] = field(default_factory=list)
    guidance: list[Guidance] = field(default_factory=list)
    secrets: list[Secret] = field(default_factory=list)
    scenarios: list[ScenarioSpec] = field(default_factory=list)
    url_mappings: dict[str, str] = field(default_factory=dict)
    mcp_configs: list[MCPServerConfig] = field(default_factory=list)


def load_integration(manifest: IntegrationManifest, root: Path) -> LoadedIntegration:
    """Load a single integration package from disk.

    Parses guidance markdown files and constructs secret/guidance entries
    in the format expected by the sync pipeline.
    """
    errors = validate_integration(manifest, root)
    if errors:
        logger.warning(
            "Integration '%s' has validation issues:\n  %s",
            manifest.slug,
            "\n  ".join(errors),
        )

    result = LoadedIntegration(manifest=manifest, root_dir=root)

    functions_dir = root / "functions"
    if functions_dir.is_dir():
        result.function_dir = functions_dir

    venvs_dir = root / "venvs"
    if venvs_dir.is_dir():
        result.venv_dir = venvs_dir

    guidance_dir = root / "guidance"
    if guidance_dir.is_dir():
        result.guidance_entries = _load_guidance(guidance_dir)

    if manifest.secrets:
        result.secret_entries = [
            Secret(name=s.name, value="", description=s.description)
            for s in manifest.secrets
        ]

    if manifest.scenarios:
        scenarios_dir = root / "scenarios"
        result.scenario_specs = [
            load_scenario(scenarios_dir / scenario_file)
            for scenario_file in manifest.scenarios
        ]

    if manifest.demo_site is not None:
        demo_dir = root / manifest.demo_site.dir
        if demo_dir.is_dir():
            result.url_mapping = {manifest.demo_site.url_mapping: manifest.slug}

    if manifest.mcp is not None:
        result.mcp_config = manifest.mcp

    return result


def load_integrations(
    slugs: list[str],
    search_paths: list[Path],
) -> AggregatedIntegrations:
    """Load and merge multiple integration packages by slug.

    Searches ``search_paths`` in order for directories matching each slug.
    The first match wins for each slug.
    """
    available: dict[str, tuple[IntegrationManifest, Path]] = {}
    for search_path in search_paths:
        if not search_path.is_dir():
            continue
        for candidate in search_path.iterdir():
            if not candidate.is_dir():
                continue
            manifest_file = candidate / "manifest.yaml"
            if not manifest_file.is_file():
                continue
            try:
                manifest = _load_manifest(manifest_file)
                if manifest.slug not in available:
                    available[manifest.slug] = (manifest, candidate)
            except Exception:
                logger.warning(
                    "Failed to parse manifest at %s",
                    manifest_file,
                    exc_info=True,
                )

    result = AggregatedIntegrations()

    for slug in slugs:
        entry = available.get(slug)
        if entry is None:
            logger.warning(
                "Integration '%s' not found in search paths: %s",
                slug,
                [str(p) for p in search_paths],
            )
            continue

        manifest, root = entry
        loaded = load_integration(manifest, root)

        if loaded.function_dir is not None:
            result.function_dirs.append(loaded.function_dir)
        if loaded.venv_dir is not None:
            result.venv_dirs.append(loaded.venv_dir)
        result.guidance.extend(loaded.guidance_entries)
        result.secrets.extend(loaded.secret_entries)
        result.scenarios.extend(loaded.scenario_specs)
        if loaded.url_mapping:
            result.url_mappings.update(loaded.url_mapping)
        if loaded.mcp_config is not None:
            result.mcp_configs.append(loaded.mcp_config)

    return result


def _load_guidance(guidance_dir: Path) -> list[Guidance]:
    """Parse guidance markdown files into canonical :class:`Guidance` models.

    Each ``.md`` file becomes a :class:`Guidance` instance with
    title derived from the filename and content from the file body.
    Files with empty bodies are skipped (Guidance requires min_length=1).
    """
    entries: list[Guidance] = []
    for md_file in sorted(guidance_dir.glob("*.md")):
        try:
            raw_content = md_file.read_text().strip()
            if not raw_content:
                logger.warning("Skipping empty guidance file: %s", md_file)
                continue
            title = md_file.stem.replace("_", " ").replace("-", " ").title()
            entries.append(Guidance(title=title, content=raw_content))
        except OSError:
            logger.warning("Failed to read guidance file: %s", md_file, exc_info=True)
    return entries
