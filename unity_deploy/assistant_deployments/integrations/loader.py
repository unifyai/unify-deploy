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

from unify.guidance_manager.custom_guidance import (
    collect_custom_guidance,
    guidance_titles_from_source,
)
from unity_deploy.assistant_deployments.integrations.discovery import (
    _load_manifest,
)
from unity_deploy.assistant_deployments.integrations.types import (
    IntegrationManifest,
    MCPServerConfig,
)
from unity_deploy.assistant_deployments.integrations.validation import (
    validate_integration,
)
from unity_deploy.assistant_deployments.scenarios.loader import load_scenario
from unity_deploy.assistant_deployments.scenarios.types import ScenarioSpec
from unify.secret_manager.types import Secret

logger = logging.getLogger(__name__)


def _stem_to_title(stem: str) -> str:
    """Mirror guidance.jsonl title transformation for registry rows."""
    return stem.replace("_", " ").replace("-", " ").title()


@dataclass
class LoadedIntegration:
    """A single integration package loaded from disk."""

    manifest: IntegrationManifest
    root_dir: Path
    function_dir: Path | None = None
    venv_dir: Path | None = None
    guidance_dir: Path | None = None
    secret_entries: list[Secret] = field(default_factory=list)
    scenario_specs: list[ScenarioSpec] = field(default_factory=list)
    url_mapping: dict[str, str] | None = None
    mcp_config: MCPServerConfig | None = None
    registry_row: dict | None = None
    """Runtime registry row for ``Integrations/Manifests``.

    Populated when the manifest has at least one capability or any secrets.
    Consumed downstream by ``_sync_integration_registry`` (deploy time) and
    ``unify.integration_status`` (runtime).  All list/dict values are
    JSON-stringified to keep the DataManager schema scalar-only.
    """


@dataclass
class AggregatedIntegrations:
    """Merged data from multiple integrations, ready for downstream consumption."""

    function_dirs: list[Path] = field(default_factory=list)
    venv_dirs: list[Path] = field(default_factory=list)
    guidance_dirs: list[Path] = field(default_factory=list)
    secrets: list[Secret] = field(default_factory=list)
    scenarios: list[ScenarioSpec] = field(default_factory=list)
    url_mappings: dict[str, str] = field(default_factory=dict)
    mcp_configs: list[MCPServerConfig] = field(default_factory=list)
    registry_rows: list[dict] = field(default_factory=list)
    """One row per loaded integration, ready for the runtime registry sync."""


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
    if (guidance_dir / "guidance.jsonl").is_file():
        result.guidance_dir = guidance_dir

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

    result.registry_row = _build_registry_row(manifest)

    return result


def _build_registry_row(manifest: IntegrationManifest) -> dict:
    """Project a manifest into a flat row for the ``Integrations/Manifests`` context.

    The row carries three lookup fields:

    * ``required_secrets`` / ``optional_secrets`` — declares which secret
      names the integration uses.
    * ``function_names`` — names of the package's ``@custom_function``
      callables, resolvable to ``function_id``s by FunctionManager.
    * ``guidance_titles`` — titles of the package's guidance entries,
      resolvable to ``guidance_id``s by GuidanceManager.

    All list/dict values are JSON-stringified — DataManager prefers scalar
    columns, and the registry is a flat data context.

    .. note::

       Post-May-2026 cleanup: the runtime side
       (:mod:`unify.integration_status`) reads disk discovery directly,
       not these persisted rows.  This row exists for telemetry — a
       per-deployment record of which integrations were declared — and
       for any future read paths that explicitly want the deployment's
       declared set vs the runtime's available set.  See
       ``_sync_integration_registry`` in ``seed_sync.py`` for the docstring
       describing this trade-off in full.
    """
    import json

    required = sorted({s.name for s in manifest.secrets if s.required})
    optional = sorted({s.name for s in manifest.secrets if not s.required})

    function_names: set[str] = set()
    guidance_titles: set[str] = set()
    capability_ids: list[str] = []
    for cap in manifest.capabilities:
        capability_ids.append(cap.id)
        function_names.update(cap.functions)
        guidance_titles.update(_stem_to_title(stem) for stem in cap.guidance)

    return {
        "slug": manifest.slug,
        "label": manifest.name,
        "category": manifest.sector,
        "version": manifest.version,
        "tier": manifest.tier,
        "quality": (
            manifest.quality.value
            if hasattr(manifest.quality, "value")
            else str(manifest.quality)
        ),
        "required_secrets_json": json.dumps(required),
        "optional_secrets_json": json.dumps(optional),
        "capability_ids_json": json.dumps(capability_ids),
        "function_names_json": json.dumps(sorted(function_names)),
        "guidance_titles_json": json.dumps(sorted(guidance_titles)),
        "tags_json": json.dumps(list(manifest.tags)),
        "homepage": manifest.homepage or "",
        "description": manifest.description,
    }


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
        if loaded.guidance_dir is not None:
            result.guidance_dirs.append(loaded.guidance_dir)
        result.secrets.extend(loaded.secret_entries)
        result.scenarios.extend(loaded.scenario_specs)
        if loaded.url_mapping:
            result.url_mappings.update(loaded.url_mapping)
        if loaded.mcp_config is not None:
            result.mcp_configs.append(loaded.mcp_config)
        if loaded.registry_row is not None:
            result.registry_rows.append(loaded.registry_row)

    return result


def guidance_titles_for_dir(guidance_dir: Path) -> list[str]:
    """Return sorted titles from a package's ``guidance.jsonl`` file."""
    return guidance_titles_from_source(collect_custom_guidance(path=guidance_dir))
