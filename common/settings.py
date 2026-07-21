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
        "production": "https://service.a.run.app",
        "staging": "https://service.a.run.app",
    },
    "adapters": {
        "production": "https://service.a.run.app",
        "staging": "https://service.a.run.app",
    },
}


def _get_deploy_env() -> str:
    """Resolve the deployment environment.

    ``DEPLOY_ENV`` is the single canonical setting, pinned explicitly on
    every deployment (Cloud Run env vars, job manifests, k8s configs).
    Returns ``"production"`` or ``"staging"``.
    """
    deploy_env = (os.environ.get("DEPLOY_ENV") or "").strip().lower()
    return "staging" if deploy_env == "staging" else "production"


def _service_url(env_var: str, service: str) -> str:
    """Resolve a service URL: env var override > deploy_env-specific default."""
    urls = _SERVICE_URLS[service]
    return os.environ.get(env_var, urls.get(_get_deploy_env(), urls["production"]))


def _image_hash_blob_name(*, deploy_env: str) -> str:
    """Resolve the GCS blob name that holds the active Unity image hash.

    Production reads ``image_hash.txt``; staging reads
    ``image_hash_staging.txt``.
    """
    if deploy_env == "production":
        return "image_hash.txt"
    return f"image_hash_{deploy_env}.txt"


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

        # GCP identifiers
        self.gcp_project_id: str = os.environ.get(
            "GCP_PROJECT_ID",
            "gcp-project-runtime",
        )
        self.default_region: str = "us-central1"
        self.gke_cluster_name: str = os.environ.get("UNITY_GKE_CLUSTER_NAME", "unity")
        self.default_namespace: str = self.deploy_env

        # Service URLs
        self.orchestra_url: str = _service_url("ORCHESTRA_URL", "orchestra")
        self.comms_url: str = _service_url("UNITY_COMMS_URL", "comms")
        self.adapters_url: str = _service_url("UNITY_ADAPTERS_URL", "adapters")
        self.task_due_queue_location: str = os.environ.get(
            "UNITY_TASK_DUE_QUEUE_LOCATION",
            self.default_region,
        )
        self.task_due_queue_name: str = os.environ.get(
            "UNITY_TASK_DUE_QUEUE_NAME",
            f"unity-task-due{self.env_suffix}",
        )
        self.task_offline_queue_name: str = os.environ.get(
            "UNITY_TASK_OFFLINE_QUEUE_NAME",
            f"unity-task-offline{self.env_suffix}",
        )
        self.task_execution_repair_queue_name: str = os.environ.get(
            "UNITY_TASK_EXECUTION_REPAIR_QUEUE_NAME",
            f"unity-task-execution-repair{self.env_suffix}",
        )
        self.task_due_dispatch_deadline_seconds: int = int(
            os.environ.get("UNITY_TASK_DUE_DISPATCH_DEADLINE_SECONDS", "30"),
        )
        self.task_execution_horizon_days: int = int(
            os.environ.get("UNITY_TASK_EXECUTION_HORIZON_DAYS", "29"),
        )
        self.offline_task_job_ttl_seconds: int = int(
            os.environ.get("UNITY_OFFLINE_TASK_JOB_TTL_SECONDS", "600"),
        )
        self.provider_event_dispatch_request_ttl_seconds: int = int(
            os.environ.get("UNITY_PROVIDER_EVENT_DISPATCH_REQUEST_TTL_SECONDS", "300"),
        )

        # Auth keys
        self.orchestra_admin_key: str = os.environ.get("ORCHESTRA_ADMIN_KEY", "")

        # Slack Events API signing secret. App-level (one value shared across
        # all workspace installs of the Slack app), set in the Slack-app
        # manifest. Used by the adapter's /slack/events webhook to HMAC-verify
        # inbound payloads before forwarding them to Orchestra. Per-workspace
        # bot tokens are stored in Orchestra (slack_installs), not here.
        self.slack_signing_secret: str = os.environ.get("SLACK_SIGNING_SECRET", "")

        # Microsoft Teams (Bot Framework) multi-tenant app credentials.
        # App-level (one registration shared across every tenant that
        # installs the Teams app). ``ms_teams_bot_app_id`` is the audience
        # the /ms-teams-bot/messages webhook validates inbound activity JWTs
        # against; ``ms_teams_bot_app_secret`` mints a Bot Connector token for
        # the one proactive send we originate here — the install-welcome DM
        # (every other outbound reply is minted+sent by the gateway). The bot
        # is a single-tenant registration in Unify's home tenant, so the token
        # is minted from ``ms365_admin_tenant_id``'s authority. Per-tenant
        # install state (service_url, conversation references) lives in
        # Orchestra (ms_teams_bot_installs).
        self.ms_teams_bot_app_id: str = os.environ.get("MS_TEAMS_BOT_APP_ID", "")
        self.ms_teams_bot_app_secret: str = os.environ.get(
            "MS_TEAMS_BOT_APP_SECRET",
            "",
        )

        # Cleanup / Workspace integration.  ``workspace_admin_subject`` is
        # the Workspace user we impersonate for Admin SDK Directory calls;
        # still used by ``DELETE /gmail/delete`` (Orchestra teardown
        # worker) and by Gmail SA-delegated send/read fallbacks.
        # ``WORKSPACE_EMAIL_DOMAIN`` and ``MS365_LICENSE_SKU_ID`` were
        # removed together with the platform mailbox provisioning
        # endpoints.
        self.job_inventory_lookback_hours: int = int(
            os.environ.get("UNITY_JOB_INVENTORY_LOOKBACK_HOURS", "36"),
        )
        self.workspace_admin_subject: str = os.environ.get(
            "WORKSPACE_ADMIN_SUBJECT",
            "dan@unify.ai",
        )

        # Microsoft 365 admin app — used by ``DELETE /outlook/delete``
        # (Orchestra teardown worker) and by app-only Graph fallbacks
        # (e.g. Teams watch teardown).  No longer used to mint tokens;
        # the platform-mailbox provisioning + ``unify_ropc`` refresh
        # paths were retired with the wider @unify.ai email feature.
        self.ms365_admin_tenant_id: str = os.environ.get(
            "MS365_ADMIN_TENANT_ID",
            "",
        )
        self.ms365_admin_client_id: str = os.environ.get(
            "MS365_ADMIN_CLIENT_ID",
            "",
        )

        # BYOD Microsoft 365 (multi-tenant Entra ID app for user-granted access)
        self.ms365_byod_client_id: str = os.environ.get(
            "MS365_BYOD_CLIENT_ID",
            "",
        )

        # BYOD Google (platform-level OAuth 2.0 web app for user-granted Gmail access)
        self.google_oauth_client_id: str = os.environ.get(
            "GOOGLE_OAUTH_CLIENT_ID",
            "",
        )

        # HMAC key shared with Orchestra for signing/verifying OAuth state params
        self.oauth_state_signing_key: str = os.environ.get(
            "OAUTH_STATE_SIGNING_KEY",
            "",
        )

        # K8s Lease-based assignment
        self.lease_duration_seconds: int = 60

        # Derived names used across the codebase
        self.unity_image_name: str = f"unity{self.env_suffix}"
        self.gmail_topic: str = f"gmail-notifications{self.env_suffix}"
        self.unity_coordinator_email_address: str = (
            (
                os.environ.get("UNITY_COORDINATOR_EMAIL_ADDRESS")
                or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_EMAIL_ADDRESS")
                or "twin@unify.ai"
            )
            .strip()
            .lower()
        )
        self.unity_coordinator_email_watch_topic: str = os.environ.get(
            "UNITY_COORDINATOR_EMAIL_WATCH_TOPIC",
            self.gmail_topic,
        )
        self.image_hash_blob: str = _image_hash_blob_name(deploy_env=self.deploy_env)
        self.client_bundle_bucket: str = os.environ.get(
            "UNITY_CLIENT_BUNDLE_BUCKET",
            "unity-client-bundles",
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
        # Retained for legacy VM discovery paths. New placements are selected
        # on demand from the GCP capability preflight, not Cloud Run env vars.
        self.vm_provisioned_locations: dict[str, str] = {
            self.vm_region: self.vm_zone,
        }
        self.vm_location_preflight_cache_ttl_seconds: float = float(
            os.environ.get("UNITY_VM_LOCATION_PREFLIGHT_CACHE_TTL_SECONDS", "300"),
        )
        if self.vm_location_preflight_cache_ttl_seconds <= 0:
            raise ValueError(
                "UNITY_VM_LOCATION_PREFLIGHT_CACHE_TTL_SECONDS must be positive",
            )

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

    def org_topic(self, organization_id: int | str) -> str:
        """Pub/Sub topic carrying an organization's chat frames.

        One topic per organization multiplexes team group-chat and human DM
        frames; Console's SSE route subscribes per user and filters frames
        server-side by that user's team memberships and DM participation.
        """
        return f"unity-org-{organization_id}{self.env_suffix}"


SETTINGS = Settings()
