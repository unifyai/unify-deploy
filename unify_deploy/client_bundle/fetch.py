"""Client deployment bundle fetch and unpack for assistant pods."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import httpx

from unify_deploy.assistant_deployments.routing_manifest import (
    BundleTarget,
    resolve_client_bundle_target,
)

logger = logging.getLogger(__name__)

# Pods only mount a writable emptyDir at /tmp; /opt is read-only on Autopilot.
DEFAULT_CLIENT_DEPLOYMENT_ROOT = Path("/tmp/client-deployment")


def client_deployment_root() -> Path | None:
    raw = (os.environ.get("CLIENT_DEPLOYMENT_ROOT") or "").strip()
    if raw:
        return Path(raw)
    return None


def bundled_client_mode() -> bool:
    return (
        os.environ.get("UNITY_DEPLOY_CLIENT_MODE") or "bundled"
    ).strip().lower() == "bundled"


def _comms_base_url() -> str:
    return (
        os.environ.get("UNITY_COMMS_URL")
        or os.environ.get("COMMS_URL")
        or "http://unity-comms-app:8080"
    ).rstrip("/")


def _bundle_api_url(*, assistant_id: int, binding_id: str | None) -> str:
    params = urlencode(
        {
            key: value
            for key, value in {
                "assistant_id": assistant_id,
                "binding_id": binding_id,
            }.items()
            if value is not None
        },
    )
    return f"{_comms_base_url()}/infra/client-bundle?{params}"


def _auth_headers() -> dict[str, str]:
    # Authenticate as this assistant, not with the platform admin key. Comms
    # verifies this UNIFY_KEY against the Orchestra assistant record, so a
    # pod (live or headless offline) can only fetch the bundle mapped to
    # its own assistant — no live AssistantSession required.
    unify_key = (os.environ.get("UNIFY_KEY") or "").strip()
    if not unify_key:
        return {}
    return {"Authorization": f"Bearer {unify_key}"}


def fetch_bundle_metadata(
    *,
    assistant_id: int,
    binding_id: str | None = None,
) -> dict:
    url = _bundle_api_url(assistant_id=assistant_id, binding_id=binding_id)
    response = httpx.get(url, headers=_auth_headers(), timeout=30.0)
    response.raise_for_status()
    return response.json()


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError(f"Bundle sha256 mismatch: expected {expected}, got {actual}")


def _download_signed_url(url: str, destination: Path) -> None:
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def unpack_bundle(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(destination)


def ensure_client_bundle(
    *,
    org_id: int | None,
    team_ids: list[int] | None,
    user_id: str | None,
    assistant_id: int | None,
    binding_id: str | None = None,
) -> BundleTarget | None:
    """Fetch and unpack the client bundle when running in bundled mode."""

    if not bundled_client_mode():
        return None

    target = resolve_client_bundle_target(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if target is None:
        return None

    if assistant_id is None:
        # The bundle endpoint authorizes per assistant session, so a concrete
        # assistant id is required to fetch. A blank-slate assistant with no
        # mapped target returns above; reaching here without an id is a bug.
        raise ValueError("assistant_id is required to fetch a client bundle")

    existing_root = client_deployment_root()
    if existing_root is not None and existing_root.is_dir():
        return target

    metadata = fetch_bundle_metadata(
        assistant_id=assistant_id,
        binding_id=binding_id,
    )
    signed_url = metadata["signed_url"]
    sha256 = metadata["sha256"]
    client_name = metadata.get("client_name", target.client_name)

    root = DEFAULT_CLIENT_DEPLOYMENT_ROOT / client_name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="client-bundle-") as tmp_dir:
        archive_path = Path(tmp_dir) / f"{client_name}.tar.gz"
        _download_signed_url(signed_url, archive_path)
        _verify_sha256(archive_path, sha256)
        unpack_bundle(archive_path, root)

    os.environ["CLIENT_DEPLOYMENT_ROOT"] = str(root)
    logger.info(
        "Fetched client bundle client=%s deployment=%s root=%s",
        client_name,
        target.deployment,
        root,
    )
    return target
