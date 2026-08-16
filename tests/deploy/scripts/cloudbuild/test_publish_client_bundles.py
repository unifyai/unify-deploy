"""Tests for the client-bundle publish step used by cloudbuild.yaml.

Covers:
- ``publish_client_bundles.sh`` builds tarballs, computes sha256, and
  publishes to ``gsutil`` under the SHA it is given — verified with a real
  subprocess invocation against a local ``gsutil`` shim standing in for the
  GCS boundary the test sandbox cannot reach.
- both cloudbuild yaml files use a real published builder image
  (``gcr.io/cloud-builders/gsutil``, not the nonexistent
  ``gcr.io/cloud-builders/bash``) for the ``publish-client-bundles`` step and
  pass Cloud Build's ``${COMMIT_SHA}`` substitution as the SHA argument.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "deploy/scripts/cloudbuild/publish_client_bundles.sh"

_GSUTIL_SHIM = """#!/usr/bin/env bash
set -euo pipefail
dest="${@: -1}"
local_dest="${FAKE_GCS_ROOT}/${dest#gs://}"
mkdir -p "$(dirname "$local_dest")"
if [ "$2" = "-" ]; then
  cat > "$local_dest"
else
  cp "$2" "$local_dest"
fi
"""


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _install_gsutil_shim(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Return (env, fake_gcs_root) with a `gsutil` shim installed on PATH."""
    fake_gcs_root = tmp_path / "gcs"
    fake_gcs_root.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    gsutil = shim_dir / "gsutil"
    gsutil.write_text(_GSUTIL_SHIM, encoding="utf-8")
    gsutil.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
        "FAKE_GCS_ROOT": str(fake_gcs_root),
        "UNIFY_CLIENT_BUNDLE_BUCKET": "test-bucket",
    }
    return env, fake_gcs_root


def test_publishes_expected_objects_using_explicit_sha_argument(tmp_path: Path) -> None:
    env, fake_gcs_root = _install_gsutil_shim(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "staging", "deadbeef1234"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    published = fake_gcs_root / "test-bucket/staging/client_alpha"
    tarball = published / "deadbeef1234.tar.gz"
    assert tarball.is_file()
    assert (published / "latest.txt").read_text(encoding="utf-8") == "deadbeef1234"
    assert (published / "deadbeef1234.sha256").read_text(
        encoding="utf-8",
    ) == hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert "Published client_alpha bundle deadbeef1234" in result.stdout


def test_falls_back_to_git_rev_parse_head_when_sha_argument_omitted(
    tmp_path: Path,
) -> None:
    env, fake_gcs_root = _install_gsutil_shim(tmp_path)

    subprocess.run(
        ["bash", str(SCRIPT), "staging"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    latest_file = fake_gcs_root / "test-bucket/staging/client_alpha/latest.txt"
    assert latest_file.read_text(encoding="utf-8") == expected_sha


def test_cloudbuild_staging_publish_client_bundles_uses_real_builder_image() -> None:
    config = _read("deploy/cloudbuild-staging.yaml")
    assert "id: 'publish-client-bundles'" in config
    assert "gcr.io/cloud-builders/bash" not in config
    assert "gcr.io/cloud-builders/gsutil" in config
    assert "publish_client_bundles.sh', 'staging', '${COMMIT_SHA}'" in config


def test_cloudbuild_production_publish_client_bundles_uses_real_builder_image() -> None:
    config = _read("deploy/cloudbuild.yaml")
    assert "id: 'publish-client-bundles'" in config
    assert "gcr.io/cloud-builders/bash" not in config
    assert "gcr.io/cloud-builders/gsutil" in config
    assert "publish_client_bundles.sh', 'production', '${COMMIT_SHA}'" in config
