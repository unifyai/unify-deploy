"""
Centralized configuration for the Communication service.

Every setting resolves from the environment at the moment it is read, so a
value exported after this module is imported is still the value callers see::

    from common.settings import SETTINGS

    namespace = SETTINGS.default_namespace

Reading them once into instance state instead made configuration a snapshot of
the environment as it stood when whichever module imported this one first ran.
Import order is not something callers choose -- under pytest it follows
alphabetical collection -- so that snapshot silently decided things like
whether Pub/Sub topic names carried the ``-staging`` suffix, and a suite could
pass file-by-file and fail as a directory.

``load_dotenv`` runs at import so ``.env`` values are present in the
environment before anything reads a setting.
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
    """Read-only configuration resolved from environment variables on access.

    ``deploy_env`` is the canonical environment identifier:
    ``"production"`` or ``"staging"``.  Use it everywhere instead
    of the legacy ``is_staging`` boolean.

    The class holds no environment-derived state.  Settings that never depend
    on the environment are plain class attributes; everything else is a
    property, so tests set the variable (``monkeypatch.setenv``) rather than
    assigning to the attribute.
    """

    # Fixed infrastructure identifiers, the same in every environment.
    default_region: str = "us-central1"
    vm_project_id: str = "gcp-project-vms"
    dns_project_id: str = "gcp-project-dns"
    vm_region: str = "us-central1"

    # K8s Lease-based assignment
    lease_duration_seconds: int = 60

    # AssistantSession control plane
    assistant_session_group: str = "infra.unify.ai"
    assistant_session_version: str = "v1alpha1"
    assistant_session_plural: str = "assistantsessions"
    assistant_session_kind: str = "AssistantSession"
    assistant_session_protocol_version: str = "v1"

    @property
    def deploy_env(self) -> str:
        return _get_deploy_env()

    @property
    def env_suffix(self) -> str:
        deploy_env = self.deploy_env
        return f"-{deploy_env}" if deploy_env != "production" else ""

    # GCP identifiers

    @property
    def gcp_project_id(self) -> str:
        return os.environ.get("GCP_PROJECT_ID", "gcp-project-runtime")

    @property
    def gke_cluster_name(self) -> str:
        return os.environ.get("UNITY_GKE_CLUSTER_NAME", "unity")

    @property
    def default_namespace(self) -> str:
        return self.deploy_env

    # Service URLs

    @property
    def orchestra_url(self) -> str:
        return _service_url("ORCHESTRA_URL", "orchestra")

    @property
    def comms_url(self) -> str:
        return _service_url("UNITY_COMMS_URL", "comms")

    @property
    def adapters_url(self) -> str:
        return _service_url("UNITY_ADAPTERS_URL", "adapters")

    @property
    def task_due_queue_location(self) -> str:
        return os.environ.get("UNITY_TASK_DUE_QUEUE_LOCATION", self.default_region)

    @property
    def task_due_queue_name(self) -> str:
        return os.environ.get(
            "UNITY_TASK_DUE_QUEUE_NAME",
            f"unity-task-due{self.env_suffix}",
        )

    @property
    def task_offline_queue_name(self) -> str:
        return os.environ.get(
            "UNITY_TASK_OFFLINE_QUEUE_NAME",
            f"unity-task-offline{self.env_suffix}",
        )

    @property
    def task_execution_repair_queue_name(self) -> str:
        return os.environ.get(
            "UNITY_TASK_EXECUTION_REPAIR_QUEUE_NAME",
            f"unity-task-execution-repair{self.env_suffix}",
        )

    @property
    def task_due_dispatch_deadline_seconds(self) -> int:
        return int(os.environ.get("UNITY_TASK_DUE_DISPATCH_DEADLINE_SECONDS", "30"))

    @property
    def task_execution_horizon_days(self) -> int:
        return int(os.environ.get("UNITY_TASK_EXECUTION_HORIZON_DAYS", "29"))

    @property
    def offline_task_job_ttl_seconds(self) -> int:
        return int(os.environ.get("UNITY_OFFLINE_TASK_JOB_TTL_SECONDS", "600"))

    @property
    def offline_task_max_runtime_seconds(self) -> int:
        """The longest an offline run may hold a pod, whatever its task asked
        for.

        ``max_runtime_seconds`` is per-task, defaults to None, nothing sets it,
        and None meant unbounded -- so every offline job in the fleet ran with
        no ceiling at all. Five were found still going eight days past their
        scheduled moment, one looping LLM calls inside its storage-review pass
        long after the task itself had failed.

        Twelve hours is well past any legitimate run and matches the
        maintenance sweep's own staleness cutoff, so a job cannot outlive the
        sweep that would have reported it. A task needing less sets its own
        max_runtime_seconds; it cannot set more.
        """
        return int(os.environ.get("UNITY_OFFLINE_TASK_MAX_RUNTIME_SECONDS", "43200"))

    @property
    def provider_event_dispatch_request_ttl_seconds(self) -> int:
        return int(
            os.environ.get("UNITY_PROVIDER_EVENT_DISPATCH_REQUEST_TTL_SECONDS", "300"),
        )

    # Auth keys

    @property
    def orchestra_admin_key(self) -> str:
        return os.environ.get("ORCHESTRA_ADMIN_KEY", "")

    @property
    def recall_relay_secret(self) -> str:
        """Recall.ai realtime relay shared secret.

        Recall opens the websocket outbound to us and authenticates with a
        query-parameter token (it cannot present a header), so this shared
        secret is the only credential on that socket. Empty disables the relay
        outright rather than accepting anonymous connections that could publish
        into any assistant's LiveKit room.
        """
        return os.environ.get("RECALL_RELAY_SECRET", "")

    @property
    def native_google_webhook_secret(self) -> str:
        """HMAC secret for the Google Workspace Events bridge.

        Google delivers Meet/Drive/Chat events only via Cloud Pub/Sub; the push
        subscription attaches an OIDC token minted for
        ``workspace_events_push_auth_service_account`` with the Adapters base
        URL as audience, which the bridge verifies before re-emitting a
        Unify-shaped, HMAC-signed body. This holds the same value Orchestra
        verifies with (env:NATIVE_GOOGLE_WEBHOOK_SECRET).
        """
        return os.environ.get("NATIVE_GOOGLE_WEBHOOK_SECRET", "")

    @property
    def native_microsoft_webhook_secret(self) -> str:
        """HMAC secret for the Microsoft Graph native trigger bridge.

        The same secret Orchestra uses for HMAC verify and Graph clientState
        (env:NATIVE_MICROSOFT_WEBHOOK_SECRET).
        """
        return os.environ.get("NATIVE_MICROSOFT_WEBHOOK_SECRET", "")

    @property
    def workspace_events_push_auth_service_account(self) -> str:
        return os.environ.get(
            "UNITY_WORKSPACE_EVENTS_PUSH_AUTH_SA",
            f"comm-sa@{self.gcp_project_id}.iam.gserviceaccount.com",
        )

    @property
    def slack_signing_secret(self) -> str:
        """Slack Events API signing secret.

        App-level (one value shared across all workspace installs of the Slack
        app), set in the Slack-app manifest. Used by the adapter's
        /slack/events webhook to HMAC-verify inbound payloads before forwarding
        them to Orchestra. Per-workspace bot tokens are stored in Orchestra
        (slack_installs), not here.
        """
        return os.environ.get("SLACK_SIGNING_SECRET", "")

    @property
    def ms_teams_bot_app_id(self) -> str:
        """Audience the /ms-teams-bot/messages webhook validates activity JWTs
        against.

        App-level: one Bot Framework registration shared across every tenant
        that installs the Teams app. Per-tenant install state (service_url,
        conversation references) lives in Orchestra (ms_teams_bot_installs).
        """
        return os.environ.get("MS_TEAMS_BOT_APP_ID", "")

    @property
    def ms_teams_bot_app_secret(self) -> str:
        """Mints a Bot Connector token for the one proactive send originated
        here -- the install-welcome DM.

        Every other outbound reply is minted and sent by the gateway. The bot is
        a single-tenant registration in Unify's home tenant, so the token is
        minted from ``ms365_admin_tenant_id``'s authority.
        """
        return os.environ.get("MS_TEAMS_BOT_APP_SECRET", "")

    # Cleanup / Workspace integration.  ``workspace_admin_subject`` is the
    # Workspace user we impersonate for Admin SDK Directory calls; still used
    # by ``DELETE /gmail/delete`` (Orchestra teardown worker) and by Gmail
    # SA-delegated send/read fallbacks.  ``WORKSPACE_EMAIL_DOMAIN`` and
    # ``MS365_LICENSE_SKU_ID`` were removed together with the platform mailbox
    # provisioning endpoints.

    @property
    def job_inventory_lookback_hours(self) -> int:
        return int(os.environ.get("UNITY_JOB_INVENTORY_LOOKBACK_HOURS", "36"))

    @property
    def workspace_admin_subject(self) -> str:
        return os.environ.get("WORKSPACE_ADMIN_SUBJECT", "dan@unify.ai")

    # Microsoft 365 admin app -- used by ``DELETE /outlook/delete`` (Orchestra
    # teardown worker) and by app-only Graph fallbacks (e.g. Teams watch
    # teardown).  No longer used to mint tokens; the platform-mailbox
    # provisioning + ``unify_ropc`` refresh paths were retired with the wider
    # @unify.ai email feature.

    @property
    def ms365_admin_tenant_id(self) -> str:
        return os.environ.get("MS365_ADMIN_TENANT_ID", "")

    @property
    def ms365_admin_client_id(self) -> str:
        return os.environ.get("MS365_ADMIN_CLIENT_ID", "")

    @property
    def ms365_byod_client_id(self) -> str:
        """BYOD Microsoft 365 multi-tenant Entra ID app for user-granted
        access."""
        return os.environ.get("MS365_BYOD_CLIENT_ID", "")

    @property
    def google_oauth_client_id(self) -> str:
        """BYOD Google platform-level OAuth 2.0 web app for user-granted Gmail
        access."""
        return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")

    @property
    def oauth_state_signing_key(self) -> str:
        """HMAC key shared with Orchestra for signing/verifying OAuth state
        params."""
        return os.environ.get("OAUTH_STATE_SIGNING_KEY", "")

    # Derived names used across the codebase

    @property
    def unity_image_name(self) -> str:
        return f"unity{self.env_suffix}"

    @property
    def gmail_topic(self) -> str:
        return f"gmail-notifications{self.env_suffix}"

    @property
    def unity_coordinator_email_address(self) -> str:
        return (
            (
                os.environ.get("UNITY_COORDINATOR_EMAIL_ADDRESS")
                or os.environ.get("ORCHESTRA_UNITY_COORDINATOR_EMAIL_ADDRESS")
                or "twin@unify.ai"
            )
            .strip()
            .lower()
        )

    @property
    def unity_coordinator_email_watch_topic(self) -> str:
        return os.environ.get("UNITY_COORDINATOR_EMAIL_WATCH_TOPIC", self.gmail_topic)

    @property
    def unity_twin_alias_email_domain(self) -> str:
        """Catch-all domain for multiplayer twin alias email.

        Inbound routes by the recipient alias; Gmail operations delegate to
        ``unity_twin_alias_mailbox``.
        """
        return (
            (
                os.environ.get("UNITY_TWIN_ALIAS_EMAIL_DOMAIN")
                or os.environ.get("ORCHESTRA_UNITY_TWIN_ALIAS_EMAIL_DOMAIN")
                or "twins.unify.ai"
            )
            .strip()
            .lower()
        )

    @property
    def unity_twin_alias_mailbox(self) -> str:
        """Workspace mailbox that twin alias deliveries land in."""
        return (
            (os.environ.get("UNITY_TWIN_ALIAS_MAILBOX") or "twins@unify.ai")
            .strip()
            .lower()
        )

    @property
    def image_hash_blob(self) -> str:
        return _image_hash_blob_name(deploy_env=self.deploy_env)

    @property
    def client_bundle_bucket(self) -> str:
        return os.environ.get("UNITY_CLIENT_BUNDLE_BUCKET", "unity-client-bundles")

    @property
    def image_registry(self) -> str:
        """Container image registry (Artifact Registry)."""
        return f"us-central1-docker.pkg.dev/{self.gcp_project_id}/unity"

    # VM infrastructure (dedicated GCP project, separate from GKE)

    @property
    def vm_zone(self) -> str:
        return {
            "production": "us-central1-f",
            "staging": "us-central1-a",
        }.get(self.deploy_env, "us-central1-f")

    @property
    def vm_provisioned_locations(self) -> dict[str, str]:
        """Retained for legacy VM discovery paths.

        New placements are selected on demand from the GCP capability
        preflight, not Cloud Run env vars.
        """
        return {self.vm_region: self.vm_zone}

    @property
    def vm_location_preflight_cache_ttl_seconds(self) -> float:
        ttl = float(
            os.environ.get("UNITY_VM_LOCATION_PREFLIGHT_CACHE_TTL_SECONDS", "300"),
        )
        if ttl <= 0:
            raise ValueError(
                "UNITY_VM_LOCATION_PREFLIGHT_CACHE_TTL_SECONDS must be positive",
            )
        return ttl

    # Tunnel relay service

    @property
    def tunnel_subdomain(self) -> str:
        deploy_env = self.deploy_env
        if deploy_env == "production":
            return "tunnel.unify.ai"
        return f"{deploy_env}.tunnel.unify.ai"

    @property
    def tunnel_vm_name(self) -> str:
        return f"unity-tunnel-server{self.env_suffix}"

    @property
    def tunnel_gcs_bucket(self) -> str:
        return f"unity-tunnel-config{self.env_suffix}"

    @property
    def meet_screenshare_bucket(self) -> str:
        """Shared screens captured from browser meetings.

        GCS rather than process memory because the Cloud Run instance holding a
        bot's websocket is not the one an assistant pod's poll reaches. One
        overwritten object per room; provision with a short lifecycle rule.

        One bucket for both environments, separated by a ``deploy_env`` path
        prefix, matching call recordings (``unity-call-recordings``). No
        ``env_suffix`` here: the environment lives in the object path, not the
        bucket name.
        """
        return os.environ.get(
            "UNITY_MEET_SCREENSHARE_BUCKET",
            "unity-recall-meet-screenshare",
        )

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
