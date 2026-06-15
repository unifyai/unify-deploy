"""Explicit Pub/Sub credential helpers for integration tests.

These tests run outside the deployed Communication runtime, so they cannot rely
on the production-only ``GCP_SA_KEY`` convention alone. This module resolves the
best available local credential source in a deterministic order, attaches the
resolved principal to the Pub/Sub clients, and exposes that context for failure
artifacts and clearer auth diagnostics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth import default as google_auth_default
from google.cloud import pubsub_v1
from google.oauth2 import service_account

PUBSUB_CREDENTIAL_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
PUBSUB_INLINE_CREDENTIAL_ENV_VARS = ("TEST_GCP_SA_KEY", "GCP_SA_KEY")
PUBSUB_FILE_CREDENTIAL_ENV_VARS = (
    "TEST_GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
_PUBSUB_CLIENT_CONTEXT_ATTR = "_integration_pubsub_credentials"


@dataclass(frozen=True)
class ResolvedPubSubCredentials:
    """Describe the credentials chosen for local Pub/Sub integration tests."""

    credentials: Any
    source: str
    principal: str | None
    project_id: str | None
    credential_type: str
    source_detail: str | None = None

    def as_context(self) -> dict[str, str | None]:
        """Return a stable, JSON-serializable credential summary."""

        return {
            "source": self.source,
            "source_detail": self.source_detail,
            "principal": self.principal,
            "project_id": self.project_id,
            "credential_type": self.credential_type,
        }

    def describe(self) -> str:
        """Return a short human-readable summary for logs and assertions."""

        source = self.source
        if self.source_detail:
            source = f"{source} ({self.source_detail})"
        principal = self.principal or "unknown principal"
        project_id = self.project_id or "unknown project"
        return (
            f"{principal} via {source} "
            f"[type={self.credential_type}, project={project_id}]"
        )


def _resolved_from_inline_json(
    env_name: str,
    raw_json: str,
) -> ResolvedPubSubCredentials:
    """Parse inline service-account JSON from one environment variable."""

    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{env_name} must contain valid JSON service-account credentials.",
        ) from exc

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=PUBSUB_CREDENTIAL_SCOPES,
    )
    credential_type = str(info.get("type") or credentials.__class__.__name__)
    return ResolvedPubSubCredentials(
        credentials=credentials,
        source=env_name,
        principal=info.get("client_email"),
        project_id=info.get("project_id"),
        credential_type=credential_type,
    )


def _resolved_from_service_account_file(
    env_name: str,
    raw_path: str,
) -> ResolvedPubSubCredentials:
    """Load service-account credentials from an explicit file path."""

    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{env_name} points to a missing file: {path}")

    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(
            f"{env_name} must point to a valid JSON service-account file: {path}",
        ) from exc
    if not isinstance(payload, dict) or payload.get("type") != "service_account":
        raise RuntimeError(
            f"{env_name} must point to a service-account JSON file: {path}",
        )

    credentials = service_account.Credentials.from_service_account_file(
        str(path),
        scopes=PUBSUB_CREDENTIAL_SCOPES,
    )
    principal = payload.get("client_email") or getattr(
        credentials,
        "service_account_email",
        None,
    )
    project_id = payload.get("project_id")

    return ResolvedPubSubCredentials(
        credentials=credentials,
        source=env_name,
        source_detail=str(path),
        principal=principal,
        project_id=project_id,
        credential_type="service_account",
    )


def resolve_pubsub_credentials() -> ResolvedPubSubCredentials:
    """Resolve explicit Pub/Sub credentials for integration tests.

    Resolution order:
    1. ``PUBSUB_EMULATOR_HOST`` (anonymous credentials; emulator ignores auth)
    2. ``TEST_GCP_SA_KEY``
    3. ``GCP_SA_KEY``
    4. ``TEST_GOOGLE_APPLICATION_CREDENTIALS``
    5. ``GOOGLE_APPLICATION_CREDENTIALS``
    6. Ambient ADC via ``google.auth.default()``
    """

    if os.getenv("PUBSUB_EMULATOR_HOST"):
        from google.auth import credentials as auth_credentials

        project_id = (
            os.getenv("TEST_GCP_PROJECT_ID")
            or os.getenv("GCP_PROJECT_ID")
            or "local-test-project"
        )
        return ResolvedPubSubCredentials(
            credentials=auth_credentials.AnonymousCredentials(),
            source="PUBSUB_EMULATOR_HOST",
            principal=None,
            project_id=project_id,
            credential_type="anonymous",
        )

    for env_name in PUBSUB_INLINE_CREDENTIAL_ENV_VARS:
        raw_json = os.getenv(env_name)
        if raw_json:
            return _resolved_from_inline_json(env_name, raw_json)

    for env_name in PUBSUB_FILE_CREDENTIAL_ENV_VARS:
        raw_path = os.getenv(env_name)
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_file():
            if os.getenv("PUBSUB_EMULATOR_HOST"):
                continue
        return _resolved_from_service_account_file(env_name, raw_path)

    credentials, project_id = google_auth_default(scopes=PUBSUB_CREDENTIAL_SCOPES)
    return ResolvedPubSubCredentials(
        credentials=credentials,
        source="google.auth.default()",
        principal=getattr(credentials, "service_account_email", None),
        project_id=project_id,
        credential_type=credentials.__class__.__name__,
    )


def _attach_credential_context(
    client: pubsub_v1.PublisherClient | pubsub_v1.SubscriberClient,
    resolved: ResolvedPubSubCredentials,
) -> None:
    """Attach the resolved credential summary to a Pub/Sub client instance."""

    setattr(client, _PUBSUB_CLIENT_CONTEXT_ATTR, resolved.as_context())


def client_credential_context(
    client: pubsub_v1.PublisherClient | pubsub_v1.SubscriberClient,
) -> dict[str, str | None] | None:
    """Return the credential context attached to a Pub/Sub client, if any."""

    return getattr(client, _PUBSUB_CLIENT_CONTEXT_ATTR, None)


def build_pubsub_publisher_client(
    resolved: ResolvedPubSubCredentials,
) -> pubsub_v1.PublisherClient:
    """Create a PublisherClient with explicit credentials and attached context."""

    client = pubsub_v1.PublisherClient(credentials=resolved.credentials)
    _attach_credential_context(client, resolved)
    return client


def build_pubsub_subscriber_client(
    resolved: ResolvedPubSubCredentials,
) -> pubsub_v1.SubscriberClient:
    """Create a SubscriberClient with explicit credentials and attached context."""

    client = pubsub_v1.SubscriberClient(credentials=resolved.credentials)
    _attach_credential_context(client, resolved)
    return client
