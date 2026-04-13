"""
Centralized configuration for the Communication service.

All environment-based settings are read once at import time and exposed
via the module-level ``SETTINGS`` instance.  Other modules should import
from here instead of calling ``os.getenv`` directly::

    from common.settings import SETTINGS

    namespace = SETTINGS.default_namespace

``load_dotenv`` is called before ``Settings`` is instantiated so that
``.env`` values are available regardless of import order (the app's
``main.py`` calls ``load_dotenv`` after its imports, which would be too
late for module-level singletons).
"""

import os

from dotenv import load_dotenv

load_dotenv()


_SERVICE_URLS: dict[str, dict[str, str]] = {
    "orchestra": {
        "production": "https://api.unify.ai/v0",
        "staging": "https://internal.example.com/v0",
    },
    "comms": {
        "production": "https://unity-comms-app-000000000000.us-central1.run.app",
        "staging": "https://unity-comms-app-staging-000000000000.us-central1.run.app",
    },
    "adapters": {
        "production": "https://service.a.run.app",
        "staging": "https://service.a.run.app",
    },
}


def _get_deploy_env() -> str:
    """Resolve the deployment environment.

    Checks ``DEPLOY_ENV`` first (set by Cloud Run env vars on all
    deployments), then falls back to ``STAGING=true`` (legacy).
    Returns ``"production"`` or ``"staging"``.
    """
    deploy_env = (os.environ.get("DEPLOY_ENV") or "").strip().lower()
    if deploy_env == "staging":
        return deploy_env
    if os.environ.get("STAGING", "false").lower() == "true":
        return "staging"
    return "production"


def _service_url(env_var: str, service: str) -> str:
    """Resolve a service URL: env var override > deploy_env-specific default."""
    urls = _SERVICE_URLS[service]
    return os.environ.get(env_var, urls.get(_get_deploy_env(), urls["production"]))


class Settings:
    """Read-only configuration populated from environment variables.

    ``deploy_env`` is the canonical environment identifier:
    ``"production"`` or ``"staging"``.  Use it everywhere instead
    of the legacy ``is_staging`` boolean.
    """

    def __init__(self) -> None:
        self.deploy_env: str = _get_deploy_env()
        self.env_suffix: str = (
            f"-{self.deploy_env}" if self.deploy_env != "production" else ""
        )

        # Backward compatibility: True for any non-production environment.
        # Prefer checking deploy_env directly for environment-specific logic.
        self.staging: bool = self.deploy_env != "production"

        # GCP identifiers
        self.gcp_project_id: str = os.environ.get(
            "GCP_PROJECT_ID",
            "gcp-project-runtime",
        )
        self.default_region: str = "us-central1"
        self.default_namespace: str = self.deploy_env

        # Service URLs
        self.orchestra_url: str = _service_url("ORCHESTRA_URL", "orchestra")
        self.comms_url: str = _service_url("UNITY_COMMS_URL", "comms")
        self.adapters_url: str = _service_url("UNITY_ADAPTERS_URL", "adapters")

        # Auth keys
        self.orchestra_admin_key: str = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        self.shared_unify_key: str = os.environ.get("SHARED_UNIFY_KEY", "")

        # Cleanup / Workspace integration
        self.job_inventory_lookback_hours: int = int(
            os.environ.get("UNITY_JOB_INVENTORY_LOOKBACK_HOURS", "36"),
        )
        self.workspace_email_domain: str = os.environ.get(
            "WORKSPACE_EMAIL_DOMAIN",
            "unify.ai",
        )
        self.workspace_admin_subject: str = os.environ.get(
            "WORKSPACE_ADMIN_SUBJECT",
            "dan@unify.ai",
        )

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

        # Container image registry (Artifact Registry)
        self.image_registry: str = (
            f"us-central1-docker.pkg.dev/{self.gcp_project_id}/unity"
        )

        # VM infrastructure (dedicated GCP project, separate from GKE)
        self.vm_project_id: str = "gcp-project-vms"
        self.dns_project_id: str = "gcp-project-dns"
        self.vm_region: str = "us-central1"
        _zone_map = {
            "production": "us-central1-f",
            "staging": "us-central1-a",
        }
        self.vm_zone: str = _zone_map.get(self.deploy_env, "us-central1-f")

        # Tunnel relay service
        self.tunnel_subdomain: str = (
            "tunnel.unify.ai"
            if self.deploy_env == "production"
            else f"{self.deploy_env}.tunnel.unify.ai"
        )
        self.tunnel_vm_name: str = f"unity-tunnel-server{self.env_suffix}"
        self.tunnel_gcs_bucket: str = f"unity-tunnel-config{self.env_suffix}"

        # AssistantSession control plane
        self.assistant_session_group: str = "infra.unify.ai"
        self.assistant_session_version: str = "v1alpha1"
        self.assistant_session_plural: str = "assistantsessions"
        self.assistant_session_kind: str = "AssistantSession"
        self.assistant_session_protocol_version: str = "v1"

    def assistant_topic(self, assistant_id: str) -> str:
        """Pub/Sub topic name for a specific assistant.

        Uses env_suffix to match the topic name created by Orchestra's
        ``_env_suffix(deploy_env)`` (e.g., ``-staging`` or empty for
        production).
        """
        return f"unity-{assistant_id}{self.env_suffix}"


SETTINGS = Settings()
