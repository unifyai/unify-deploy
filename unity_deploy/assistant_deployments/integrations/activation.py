"""Resolve enabled integration slugs into runtime deployment payloads.

Search paths preserve the integration ownership model:
generic platform packages first, private client packages second, and mock
packages only when explicitly requested or a mock slug is enabled.
"""

from __future__ import annotations

import logging
from pathlib import Path

from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unity_deploy.assistant_deployments.integrations.discovery import (
    _BUILTIN_DIR,
    _CLIENT_DIR,
    _MOCK_DIR,
)
from unity_deploy.assistant_deployments.integrations.loader import load_integrations

logger = logging.getLogger(__name__)


def expand_integrations(
    resolved: ResolvedAssistantDeployment,
    *,
    search_paths: list[Path] | None = None,
    include_mock_packages: bool | None = None,
) -> ResolvedAssistantDeployment:
    """Merge enabled integration assets into an already-resolved assistant_deployments.

    Deployment and seed-layer resolution only produces integration slugs. This
    function expands those slugs into the concrete assets the existing startup
    pipeline already knows how to sync: guidance, secrets, function dirs, venv
    dirs, URL mappings, and MCP configs.
    """
    if not resolved.integrations:
        return resolved

    if search_paths is not None:
        paths = list(search_paths)
    else:
        # Generic packages are reusable provider/platform connectors. Client
        # packages are private real connectors or compositions for one client.
        paths = [_BUILTIN_DIR, _CLIENT_DIR]
        use_mock_packages = (
            any(slug.endswith("_mock") for slug in resolved.integrations)
            if include_mock_packages is None
            else include_mock_packages
        )
        if use_mock_packages:
            paths.append(_MOCK_DIR)
    loaded = load_integrations(resolved.integrations, paths)

    resolved.function_dirs.extend(loaded.function_dirs)
    resolved.venv_dirs.extend(loaded.venv_dirs)
    resolved.guidance.extend(loaded.guidance)
    resolved.secrets.extend(loaded.secrets)
    resolved.url_mappings.update(loaded.url_mappings)
    resolved.mcp_configs.extend(loaded.mcp_configs)
    resolved.scenarios.extend(loaded.scenarios)

    logger.info(
        "Expanded %d integration(s): %s",
        len(resolved.integrations),
        resolved.integrations,
    )
    return resolved
