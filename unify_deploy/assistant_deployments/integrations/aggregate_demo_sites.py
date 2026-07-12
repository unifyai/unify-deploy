"""Aggregate integration demo sites into agent-service/demo-sites/.

CLI tool and library function that copies integration-bundled demo site
directories into the agent-service discovery path. This avoids any runtime
changes to agent-service — it discovers demo sites the same way as before.

Usage (build-time):
    python -m unify_deploy.assistant_deployments.integrations.aggregate_demo_sites \\
        --search-paths /app/unify_deploy/assistant_deployments/integrations/packages /app/extra/integrations \\
        --target /app/agent-service/demo-sites

Usage (local dev):
    python -m unify_deploy.assistant_deployments.integrations.aggregate_demo_sites \\
        --search-paths ./unify_deploy/assistant_deployments/integrations/packages \\
        --target ./agent-service/demo-sites
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def aggregate_demo_sites(
    search_paths: list[Path],
    target_dir: Path,
) -> dict[str, Path]:
    """Copy integration demo sites into the agent-service demo-sites directory.

    For each integration with a ``demo_site`` config in its manifest:
    1. Source: ``<integration_root>/<demo_site.dir>/``
    2. Target: ``<target_dir>/<slug>/``

    Existing directories at the target are replaced.

    Returns
    -------
    dict[str, Path]
        Mapping of slug to target path for each copied demo site.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, Path] = {}

    for search_path in search_paths:
        if not search_path.is_dir():
            logger.debug("Skipping non-existent search path: %s", search_path)
            continue

        for candidate in sorted(search_path.iterdir()):
            if not candidate.is_dir():
                continue

            manifest_file = candidate / "manifest.yaml"
            if not manifest_file.is_file():
                continue

            try:
                raw = yaml.safe_load(manifest_file.read_text())
            except Exception:
                logger.warning(
                    "Failed to parse manifest at %s",
                    manifest_file,
                    exc_info=True,
                )
                continue

            demo_site_cfg = raw.get("demo_site")
            if demo_site_cfg is None:
                continue

            slug = raw.get("slug", candidate.name)
            demo_dir_name = demo_site_cfg.get("dir", "demo_site")
            source_dir = candidate / demo_dir_name

            if not source_dir.is_dir():
                logger.warning(
                    "Integration '%s' declares demo_site.dir='%s' but %s not found",
                    slug,
                    demo_dir_name,
                    source_dir,
                )
                continue

            dest = target_dir / slug
            if dest.exists():
                shutil.rmtree(dest)

            shutil.copytree(source_dir, dest)
            copied[slug] = dest
            logger.info("Copied demo site '%s': %s -> %s", slug, source_dir, dest)

    return copied


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate integration demo sites into agent-service/demo-sites/",
    )
    parser.add_argument(
        "--search-paths",
        nargs="+",
        type=Path,
        required=True,
        help="Directories to scan for integration packages",
    )
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Target directory (agent-service/demo-sites/)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = aggregate_demo_sites(args.search_paths, args.target)
    print(f"Aggregated {len(result)} demo site(s): {list(result.keys())}")


if __name__ == "__main__":
    main()
