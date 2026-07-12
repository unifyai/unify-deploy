"""Typed Pydantic settings for GCP pipeline infrastructure.

Environment detection
---------------------
``ORCHESTRA_URL`` is the canonical source of truth for which environment
the process is running in (staging / production / development).  All
environment-dependent defaults — bucket names, Pub/Sub topic suffixes,
subscription names — are derived from it so that a single env var
controls the entire pipeline surface.  Explicit env-var overrides
(``UNITY_GCP_PIPELINE_ENVIRONMENT``, ``UNITY_GCS_ARTIFACT_BUCKET``,
etc.) still take precedence for K8s manifests and CI, but when they
are absent the ``ORCHESTRA_URL`` inference prevents cross-environment
leaks like staging dispatches uploading to the production bucket.
"""

from __future__ import annotations

import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

_BUCKET_BASE = "unity-pipeline-artifacts"


def _infer_environment() -> str:
    """Derive environment from ``ORCHESTRA_URL`` (same logic as
    ``deployment_types.detect_environment`` but inlined to avoid
    circular imports from the settings module).
    """
    url = os.environ.get("ORCHESTRA_URL", "").lower()
    if "staging" in url:
        return "staging"
    if "localhost" in url or "127.0.0.1" in url:
        return "development"
    return "production"


def _bucket_for_env(env: str) -> str:
    if env == "production":
        return _BUCKET_BASE
    return f"{_BUCKET_BASE}-{env}"


class GcsArtifactStoreSettings(BaseSettings):
    """Settings for the GCS-backed artifact store."""

    model_config = {"env_prefix": "UNITY_GCS_ARTIFACT_"}

    bucket: str = ""
    prefix: str = ""
    sa_key_json: str = ""


class PubSubQueueSettings(BaseSettings):
    """Settings for the Pub/Sub-backed work queue."""

    model_config = {"env_prefix": "UNITY_PUBSUB_"}

    project_id: str = ""
    parse_topic: str = "unity-parse"
    ingest_topic: str = "unity-ingest"
    dead_letter_topic: str = "unity-dead-letter"
    parse_subscription: str = "unity-parse-sub"
    ingest_subscription: str = "unity-ingest-sub"
    dead_letter_subscription: str = "unity-dead-letter-sub"
    sa_key_json: str = ""
    ack_deadline_seconds: int = 600
    max_messages: int = 1


class GcpPipelineSettings(BaseSettings):
    """Composite settings for the full GCP pipeline infrastructure.

    ``environment`` and ``artifact_store.bucket`` are inferred from
    ``ORCHESTRA_URL`` when not set explicitly via env vars.  This
    eliminates the class of bugs where only one of the two was
    overridden for staging, causing cross-environment leaks.
    """

    model_config = {"env_prefix": "UNITY_GCP_PIPELINE_"}

    environment: str = ""
    artifact_store: GcsArtifactStoreSettings = Field(
        default_factory=GcsArtifactStoreSettings,
    )
    pubsub: PubSubQueueSettings = Field(default_factory=PubSubQueueSettings)

    @model_validator(mode="after")
    def _apply_orchestra_defaults(self) -> "GcpPipelineSettings":
        if not self.environment:
            self.environment = _infer_environment()
        if not self.artifact_store.bucket:
            self.artifact_store.bucket = _bucket_for_env(self.environment)
        return self

    def env_suffix(self) -> str:
        """Return the environment suffix used in shared resource names.

        Matches the convention used across unity/communication:
        production -> no suffix; other envs -> ``-{env}``.
        """
        return "" if self.environment == "production" else f"-{self.environment}"

    def env_qualified_topic(self, base_topic: str) -> str:
        """Append the environment suffix to a topic name."""
        return f"{base_topic}{self.env_suffix()}"
