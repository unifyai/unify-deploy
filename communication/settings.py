"""
Centralized configuration for the Communication service.

All environment-based settings are read once at import time and exposed
via the module-level ``SETTINGS`` instance.  Other modules should import
from here instead of calling ``os.getenv`` directly::

    from communication.settings import SETTINGS

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


def _is_staging() -> bool:
    return os.environ.get("STAGING", "false").lower() == "true"


def _service_url(env_var: str, staging_url: str, prod_url: str) -> str:
    return os.environ.get(env_var, staging_url if _is_staging() else prod_url)


class Settings:
    """Read-only configuration populated from environment variables.

    Replaces the scattered ``STAGING``, ``GCP_PROJECT_ID``,
    ``ORCHESTRA_URL``, etc. that were duplicated across
    ``communication/helpers.py``, ``infra/helpers.py``,
    ``infra/views.py``, and ``infra/vm_helpers.py``.
    """

    def __init__(self) -> None:
        self.staging: bool = _is_staging()
        self.env_suffix: str = "-staging" if self.staging else ""

        # GCP identifiers
        self.gcp_project_id: str = os.environ.get(
            "GCP_PROJECT_ID",
            "gcp-project-runtime",
        )
        self.default_region: str = "us-central1"
        self.default_namespace: str = "staging" if self.staging else "production"

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
        self.adapters_url: str = os.environ.get("UNITY_ADAPTERS_URL", "")

        # Auth keys (read at access time via properties if needed, but
        # most callers already read them from os.getenv at call sites)
        self.orchestra_admin_key: str = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        self.shared_unify_key: str = os.environ.get("SHARED_UNIFY_KEY", "")

        # K8s Lease-based assignment
        self.lease_duration_seconds: int = 60

        # Pending-startup Pub/Sub queue (overflow when pool is exhausted)
        self.pending_topic: str = "unity-pending-startups" + self.env_suffix
        self.pending_sub: str = self.pending_topic + "-sub"


SETTINGS = Settings()
