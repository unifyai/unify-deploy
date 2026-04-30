"""Multi-source integration discovery.

Integration packages are intentionally separated by ownership and runtime use:

1. **Generic packages**: reusable platform/provider integrations in
   ``packages/*/manifest.yaml``.
2. **Client packages**: private real client connectors or compositions in
   ``client_packages/*/manifest.yaml``.
3. **Entry points**: externally packaged integrations registered under
   ``[project.entry-points."unity_deploy.integrations"]``.
4. **Extra paths**: explicit caller-provided roots, including opt-in mocks.

Later sources override earlier ones by slug.
"""

from __future__ import annotations

import importlib.metadata
import logging
from pathlib import Path

import yaml

from unity_deploy.customization.integrations.types import IntegrationManifest

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "unity_deploy.integrations"

_BUILTIN_DIR = Path(__file__).resolve().parent / "packages"
_CLIENT_DIR = Path(__file__).resolve().parent / "client_packages"
_MOCK_DIR = Path(__file__).resolve().parent / "mock_packages"


def discover_integrations(
    extra_paths: list[Path] | None = None,
) -> list[IntegrationManifest]:
    """Discover integration packages from all sources.

    Discovery order (later sources override earlier by slug):
    1. Generic packages: ``integrations/packages/*/manifest.yaml``
    2. Client packages: ``integrations/client_packages/*/manifest.yaml``
    3. Entry points: ``[project.entry-points."unity_deploy.integrations"]``
    4. Extra paths: additional directories supplied by the caller
    """
    manifests_by_slug: dict[str, IntegrationManifest] = {}

    for m in discover_from_directory(_BUILTIN_DIR):
        manifests_by_slug[m.slug] = m

    for m in discover_from_directory(_CLIENT_DIR):
        manifests_by_slug[m.slug] = m

    for m in discover_from_entry_points():
        manifests_by_slug[m.slug] = m

    for path in extra_paths or []:
        for m in discover_from_directory(path):
            manifests_by_slug[m.slug] = m

    return list(manifests_by_slug.values())


def discover_from_directory(path: Path) -> list[IntegrationManifest]:
    """Scan a directory for integration packages (subdirs with manifest.yaml)."""
    results: list[IntegrationManifest] = []
    if not path.is_dir():
        return results

    for candidate in sorted(path.iterdir()):
        if not candidate.is_dir():
            continue
        manifest_file = candidate / "manifest.yaml"
        if not manifest_file.is_file():
            continue
        try:
            manifest = _load_manifest(manifest_file)
            results.append(manifest)
        except Exception:
            logger.warning(
                "Failed to load manifest from %s",
                manifest_file,
                exc_info=True,
            )

    return results


def discover_from_entry_points() -> list[IntegrationManifest]:
    """Discover integrations registered via pip entry points.

    Entry points should point to a module that exposes a ``manifest_path``
    attribute (a :class:`Path` to the ``manifest.yaml``).
    """
    results: list[IntegrationManifest] = []
    try:
        eps = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        eps = importlib.metadata.entry_points().get(_ENTRY_POINT_GROUP, [])

    for ep in eps:
        try:
            module = ep.load()
            manifest_path = getattr(module, "manifest_path", None)
            if manifest_path is None:
                logger.warning(
                    "Entry point '%s' does not expose manifest_path",
                    ep.name,
                )
                continue
            manifest = _load_manifest(Path(manifest_path))
            results.append(manifest)
        except Exception:
            logger.warning(
                "Failed to load integration from entry point '%s'",
                ep.name,
                exc_info=True,
            )

    return results


def _load_manifest(path: Path) -> IntegrationManifest:
    """Parse and validate a manifest.yaml file."""
    raw = yaml.safe_load(path.read_text())
    return IntegrationManifest.model_validate(raw)
