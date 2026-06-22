"""Shared kubeconfig helper for the one-time setup scripts in this directory.

These scripts run interactively against the GKE cluster named by
``DROID_GKE_CLUSTER_NAME`` (project ``gcp-project-runtime``,
region ``us-central1``, default ``unity``) and
each one previously open-coded the same gcloud + ``load_kube_config``
incantation. They are dev-only; the production path for Droid Job
creation goes through Communication's HTTP API (see the README in
this directory).

Usage::

    from _k8s_lib import ensure_kube_config
    from kubernetes import client

    if not ensure_kube_config():
        sys.exit(1)
    api = client.AppsV1Api()  # or whichever API the caller needs

The leading underscore signals "module-private to this directory" --
``deploy/scripts/kubernetes/`` is not a real Python package; sibling
scripts find this file via Python's automatic ``sys.path[0] =
os.path.dirname(sys.argv[0])`` behavior when run as
``python create_keep_alive.py``.
"""

from __future__ import annotations

import os
import subprocess

from dotenv import load_dotenv

load_dotenv()

# Cluster identity. The cluster name is resolved from the
# DROID_GKE_CLUSTER_NAME env var (the same one the runtime reads via
# common.settings), so a rename only needs to update .env. Run
# setup_auth.sh to discover and persist it.
PROJECT_ID = "gcp-project-runtime"
REGION = "us-central1"
CLUSTER_NAME = os.environ.get("DROID_GKE_CLUSTER_NAME", "unity")


def ensure_kube_config() -> bool:
    """Authenticate to the GKE cluster and load kube config.

    Runs ``gcloud container clusters get-credentials`` (which writes
    the cluster into ``~/.kube/config``) and then
    ``kubernetes.config.load_kube_config()`` to make it available to
    the Python client. Returns ``True`` on success, ``False`` on any
    failure (with a printed error message).

    Side effect: mutates ``~/.kube/config``. This is the same thing
    the operator would do interactively; the helper just packages it
    so the sibling setup scripts stay focused on their actual job.

    The historical "elaborate" path in ``setup_k8s_config.py`` that
    built a tempfile kubeconfig from gcloud-described endpoint + CA
    cert + ``print-access-token`` was defensive overkill -- the
    simple path here is what 2 of the 3 callers already used and is
    what the Cloud Build job-watcher deploy step (in base/cloudbuild
    *.yaml) uses too. If anyone hits an auth-provider deprecation
    issue, fix it here once.
    """
    from kubernetes import config

    print(f"\N{LINK SYMBOL} Connecting to GKE cluster: {CLUSTER_NAME}")
    result = subprocess.run(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            CLUSTER_NAME,
            "--region",
            REGION,
            "--project",
            PROJECT_ID,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"\N{CROSS MARK} Failed to get cluster credentials: {result.stderr}")
        print("\N{ELECTRIC LIGHT BULB} Make sure you have:")
        print("   1. gcloud CLI installed and on PATH")
        print(f"   2. Access to the GKE '{CLUSTER_NAME}' cluster")
        print(
            "   3. A valid Application Default Credentials setup "
            "(`gcloud auth application-default login`)",
        )
        return False

    print("\N{WHITE HEAVY CHECK MARK} Got cluster credentials")
    try:
        config.load_kube_config()
    except Exception as exc:
        print(f"\N{CROSS MARK} Failed to load kube config: {exc}")
        return False
    return True


__all__ = ["CLUSTER_NAME", "PROJECT_ID", "REGION", "ensure_kube_config"]
