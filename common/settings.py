"""
Centralized configuration for the Communication service.

All environment-based settings are read once at import time and exposed
via the module-level ``SETTINGS`` instance.  Other modules should import
from here instead of calling ``os.getenv`` directly::

    from common.settings import SETTINGS

    namespace = SETTINGS.default_namespace
    topic = SETTINGS.pending_topic

``load_dotenv`` is called before ``Settings`` is instantiated so that
``.env`` values are available regardless of import order (the app's
``main.py`` calls ``load_dotenv`` after its imports, which would be too
late for module-level singletons).
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_deploy_env() -> str:
    """Resolve the deployment environment.

    Checks ``DEPLOY_ENV`` first (set by Cloud Run env vars on all
    deployments), then falls back to ``STAGING=true`` (legacy).
    Returns one of ``"production"``, ``"staging"``, or ``"preview"``.
    """
    deploy_env = (os.environ.get("DEPLOY_ENV") or "").strip().lower()
    if deploy_env in ("staging", "preview"):
        return deploy_env
    if os.environ.get("STAGING", "false").lower() == "true":
        return "staging"
    return "production"


def _service_url(env_var: str, staging_url: str, prod_url: str) -> str:
    env = _get_deploy_env()
    return os.environ.get(env_var, staging_url if env != "production" else prod_url)


class Settings:
    """Read-only configuration populated from environment variables.

    Replaces the scattered ``STAGING``, ``GCP_PROJECT_ID``,
    ``ORCHESTRA_URL``, etc. that were duplicated across
    ``communication/helpers.py``, ``infra/helpers.py``,
    ``infra/views.py``, and ``infra/vm_helpers.py``.
    """

    def __init__(self) -> None:
        self.deploy_env: str = _get_deploy_env()
        self.staging: bool = self.deploy_env != "production"
        self.env_suffix: str = (
            f"-{self.deploy_env}" if self.deploy_env != "production" else ""
        )

        # GCP identifiers
        self.gcp_project_id: str = os.environ.get(
            "GCP_PROJECT_ID",
            "gcp-project-runtime",
        )
        self.default_region: str = "us-central1"
        self.default_namespace: str = self.deploy_env

        # Service URLs
        self.orchestra_url: str = _service_url(
            "ORCHESTRA_URL",
            staging_url="https://internal.example.com/v0",
            prod_url="https://api.unify.ai/v0",
        )
        self.comms_url: str = _service_url(
            "UNITY_COMMS_URL",
            staging_url="https://unity-comms-app-staging-000000000000.us-central1.run.app",
            prod_url="https://unity-comms-app-000000000000.us-central1.run.app",
        )
        self.adapters_url: str = _service_url(
            "UNITY_ADAPTERS_URL",
            staging_url="https://service.a.run.app",
            prod_url="https://service.a.run.app",
        )

        # Auth keys (read at access time via properties if needed, but
        # most callers already read them from os.getenv at call sites)
        self.orchestra_admin_key: str = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        self.shared_unify_key: str = os.environ.get("SHARED_UNIFY_KEY", "")

        # K8s Lease-based assignment
        self.lease_duration_seconds: int = 60

        # Derived names used across the codebase
        self.unity_image_name: str = f"unity{self.env_suffix}"
        self.gmail_topic: str = f"gmail-notifications{self.env_suffix}"
        self.image_hash_blob: str = (
            "image_hash.txt"
            if not self.env_suffix
            else f"image_hash_{self.deploy_env}.txt"
        )

        # Pending-startup Pub/Sub queue (overflow when pool is exhausted)
        self.pending_topic: str = "unity-pending-startups" + self.env_suffix
        self.pending_sub: str = self.pending_topic + "-sub"

    def assistant_topic(self, assistant_id: str) -> str:
        """Pub/Sub topic name for a specific assistant.

        Uses env_suffix to match the topic name created by Orchestra's
        ``_env_suffix(deploy_env)`` (e.g., -staging, -preview, or empty
        for production).
        """
        return f"unity-{assistant_id}{self.env_suffix}"


SETTINGS = Settings()
