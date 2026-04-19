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


class GcsLedgerSettings(BaseSettings):
    """Settings for GCS-backed run and cost ledgers."""

    model_config = {"env_prefix": "UNITY_GCS_LEDGER_"}

    bucket: str = "unity-pipeline-artifacts"
    prefix: str = ""


class GcpPipelineSettings(BaseSettings):
    """Composite settings for the full GCP pipeline infrastructure."""

    model_config = {"env_prefix": "UNITY_GCP_PIPELINE_"}

    environment: str = "staging"
    artifact_store: GcsArtifactStoreSettings = Field(
        default_factory=GcsArtifactStoreSettings,
    )
    pubsub: PubSubQueueSettings = Field(default_factory=PubSubQueueSettings)
    ledger: GcsLedgerSettings = Field(default_factory=GcsLedgerSettings)

    def env_suffix(self) -> str:
        """Return the environment suffix used in shared resource names.

        Matches the convention used across unity/communication:
        production -> no suffix; other envs -> ``-{env}``.
        """
        return "" if self.environment == "production" else f"-{self.environment}"

    def env_qualified_topic(self, base_topic: str) -> str:
        """Append the environment suffix to a topic name."""
        return f"{base_topic}{self.env_suffix()}"
