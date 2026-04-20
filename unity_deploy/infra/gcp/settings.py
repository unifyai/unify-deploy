"""Typed Pydantic settings for GCP pipeline infrastructure."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class GcsArtifactStoreSettings(BaseSettings):
    """Settings for the GCS-backed artifact store."""

    model_config = {"env_prefix": "UNITY_GCS_ARTIFACT_"}

    bucket: str = "unity-pipeline-artifacts"
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
    sa_key_json: str = ""
    ack_deadline_seconds: int = 600
    max_messages: int = 1


class GcpPipelineSettings(BaseSettings):
    """Composite settings for the full GCP pipeline infrastructure.

    Run and cost ledgers intentionally share ``artifact_store`` rather
    than owning their own bucket configuration. Having two separate
    bucket settings (``UNITY_GCS_LEDGER_BUCKET`` + ``UNITY_GCS_ARTIFACT_BUCKET``)
    was a footgun: overriding only the artifact bucket for staging left
    staging ledgers silently writing into the production bucket. A single
    bucket config per environment makes cross-env leaks structurally
    impossible.
    """

    model_config = {"env_prefix": "UNITY_GCP_PIPELINE_"}

    environment: str = "staging"
    artifact_store: GcsArtifactStoreSettings = Field(
        default_factory=GcsArtifactStoreSettings,
    )
    pubsub: PubSubQueueSettings = Field(default_factory=PubSubQueueSettings)

    def env_suffix(self) -> str:
        """Return the environment suffix used in shared resource names.

        Matches the convention used across unity/communication:
        production -> no suffix; other envs -> ``-{env}``.
        """
        return "" if self.environment == "production" else f"-{self.environment}"

    def env_qualified_topic(self, base_topic: str) -> str:
        """Append the environment suffix to a topic name."""
        return f"{base_topic}{self.env_suffix()}"
