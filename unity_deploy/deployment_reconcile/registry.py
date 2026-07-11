"""Load the client deployment registry for deploy-time reconciliation.

Embedded mode (local / full install) imports client subpackages so they
self-register. Bundled mode (runtime images) has no client trees in
site-packages — especially ``unify_company``, which is a brain-submodule
symlink excluded from the runtime wheel — so the registry is built from
``routing_manifest.yaml`` plus the published GCS client bundles.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_DEFAULT_BUNDLE_BUCKET = "unity-client-bundles"


def _client_mode() -> str:
    return (os.environ.get("UNITY_DEPLOY_CLIENT_MODE") or "bundled").strip().lower()


def _bundle_bucket() -> str:
    return (
        os.environ.get("UNITY_CLIENT_BUNDLE_BUCKET") or _DEFAULT_BUNDLE_BUCKET
    ).strip()


def _download_client_bundle(
    *,
    bucket_name: str,
    environment: str,
    bundle_key: str,
    destination: Path,
) -> Path:
    """Download and unpack ``bundle_key`` for *environment* into *destination*."""

    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    pointer = bucket.blob(f"{environment}/{bundle_key}/latest.txt")
    if not pointer.exists():
        raise FileNotFoundError(
            f"Bundle pointer missing: gs://{bucket_name}/{environment}/{bundle_key}/latest.txt",
        )
    sha = pointer.download_as_text().strip()
    archive_blob = bucket.blob(f"{environment}/{bundle_key}/{sha}.tar.gz")
    if not archive_blob.exists():
        raise FileNotFoundError(
            f"Bundle archive missing: gs://{bucket_name}/{environment}/{bundle_key}/{sha}.tar.gz",
        )

    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"{bundle_key}-{sha}.tar.gz"
    archive_blob.download_to_filename(str(archive_path))
    unpack_root = destination / bundle_key
    if unpack_root.exists():
        shutil.rmtree(unpack_root)
    unpack_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(unpack_root, filter="data")
    return unpack_root


def _targets_from_manifest(
    client_cfg: Mapping[str, Any],
    *,
    environment: str,
) -> list[Any]:
    from unity_deploy.assistant_deployments.deployment_types import DeploymentTarget

    env_cfg = client_cfg.get("environments", {}).get(environment, {})
    targets: list[Any] = []
    for raw in env_cfg.get("targets", []):
        targets.append(
            DeploymentTarget(
                scope=raw["scope"],
                scope_id=(
                    str(raw["scope_id"]) if raw.get("scope_id") is not None else None
                ),
                deployment=raw["deployment"],
                missing_ok=bool(raw.get("missing_ok", False)),
            ),
        )
    return targets


def _load_bundled_registry(environment: str) -> dict[str, Any]:
    from unity_deploy.assistant_deployments.clients import ClientDeploymentEntry
    from unity_deploy.assistant_deployments.deployment_types import (
        DeploymentMapping,
        load_deployment,
    )
    from unity_deploy.assistant_deployments.routing_manifest import (
        load_routing_manifest,
    )
    from unity_deploy.client_bundle.loader import _register_bundle_package

    manifest = load_routing_manifest()
    bucket_name = _bundle_bucket()
    registry: dict[str, Any] = {}
    # Keep unpacked trees for the process lifetime so DeploymentSpec path
    # fields remain valid if a runtime-plane reconcile follows.
    tmp_root = Path(tempfile.mkdtemp(prefix="reconcile-client-bundles-"))

    for client_name, client_cfg in manifest.get("clients", {}).items():
        targets = _targets_from_manifest(client_cfg, environment=environment)
        if not targets:
            continue
        bundle_key = client_cfg.get("bundle_key", client_name)
        try:
            client_root = _download_client_bundle(
                bucket_name=bucket_name,
                environment=environment,
                bundle_key=bundle_key,
                destination=tmp_root,
            )
        except Exception:
            logger.exception(
                "Failed to fetch client bundle %s for reconcile; skipping",
                bundle_key,
            )
            continue

        _register_bundle_package(client_name, client_root)
        deployments_dir = client_root / "deployments"
        specs: dict[str, Any] = {}
        for target in targets:
            if target.deployment in specs:
                continue
            specs[target.deployment] = load_deployment(
                deployments_dir,
                target.deployment,
            )

        registry[client_name] = ClientDeploymentEntry(
            mapping=DeploymentMapping(targets=targets),
            specs=specs,
            environment=environment,
        )
        logger.info(
            "Loaded bundled client '%s' (%d deployment(s), env=%s)",
            client_name,
            len(specs),
            environment,
        )

    return registry


def load_deployment_registry(
    *,
    environment: str | None = None,
) -> Mapping[str, Any]:
    """Return client deployment entries for reconcile planning."""

    from unity_deploy.assistant_deployments.clients import (
        _CLIENT_DEPLOYMENTS,
        _ensure_embedded_clients_registered,
    )
    from unity_deploy.assistant_deployments.deployment_types import detect_environment

    env = environment or detect_environment()
    if _client_mode() == "embedded":
        _ensure_embedded_clients_registered()
        return _CLIENT_DEPLOYMENTS

    return _load_bundled_registry(env)
