"""Resolve enabled integration slugs into runtime customization payloads."""

from __future__ import annotations

import logging
from pathlib import Path

from unity_deploy.customization.clients import ResolvedCustomization
from unity_deploy.customization.integrations.discovery import _BUILTIN_DIR
from unity_deploy.customization.integrations.loader import load_integrations

logger = logging.getLogger(__name__)


def expand_integrations(
    resolved: ResolvedCustomization,
    *,
    search_paths: list[Path] | None = None,
) -> ResolvedCustomization:
    """Merge enabled integration assets into an already-resolved customization.

    Deployment and seed-layer resolution only produces integration slugs. This
    function expands those slugs into the concrete assets the existing startup
    pipeline already knows how to sync: guidance, secrets, function dirs, venv
    dirs, URL mappings, and MCP configs.
    """
    if not resolved.integrations:
        return resolved

    paths = search_paths if search_paths is not None else [_BUILTIN_DIR]
    loaded = load_integrations(resolved.integrations, paths)

    resolved.function_dirs.extend(loaded.function_dirs)
    resolved.venv_dirs.extend(loaded.venv_dirs)
    resolved.guidance.extend(loaded.guidance)
    resolved.secrets.extend(loaded.secrets)
    resolved.url_mappings.update(loaded.url_mappings)
    resolved.mcp_configs.extend(loaded.mcp_configs)

    logger.info(
        "Expanded %d integration(s): %s",
        len(resolved.integrations),
        resolved.integrations,
    )
    return resolved
