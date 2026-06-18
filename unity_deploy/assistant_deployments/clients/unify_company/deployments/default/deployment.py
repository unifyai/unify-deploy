"""Default Unify company-brain deployment seeds."""

from __future__ import annotations

from pathlib import Path

from droid.guidance_manager.types.guidance import Guidance
from droid.secret_manager.types import Secret
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec

_DIR = Path(__file__).resolve().parent


def get_deployment() -> DeploymentSpec:
    return DeploymentSpec(
        name="default",
        actor_config=ActorConfig(),
        guidance=[
            Guidance(
                title="CRM stage hygiene",
                content=(
                    "Treat Companies as the unit of pipeline progression. "
                    "Only advance a stage when there is durable evidence in "
                    "Data/CRM/CompanyEvidence, and every automatic stage change "
                    "must create a StageTransitions row with a reason."
                ),
            ),
            Guidance(
                title="CRM email sending policy",
                content=(
                    "Any external email sent from a Unify mailbox must first "
                    "create an audited OutboundActions row containing actor, "
                    "mailbox, reason, recipients, subject, and body. Default to "
                    "dry-run unless a task or human explicitly approves sending."
                ),
            ),
            Guidance(
                title="CRM evidence matching policy",
                content=(
                    "Prefer deterministic evidence: participant email, company "
                    "domain, known contact, calendar participants, and explicit "
                    "account names in titles. Treat summary-only mentions as "
                    "low-confidence review items, not automatic account links."
                ),
            ),
            Guidance(
                title="CRM document ingestion policy",
                content=(
                    "Drive and local document ingestion starts with a manifest. "
                    "Classify sensitive material before content ingestion and "
                    "record what was indexed, skipped, or restricted."
                ),
            ),
        ],
        secrets=[
            Secret(
                name="FIREFLIES_API_KEY",
                value="${FIREFLIES_API_KEY}",
                description="Fireflies API key for transcript export and CRM sync.",
            ),
            Secret(
                name="GOOGLE_SERVICE_ACCOUNT_KEY_FILE",
                value="${GOOGLE_SERVICE_ACCOUNT_KEY_FILE}",
                description="Service account key path for Gmail and Drive delegation.",
            ),
        ],
        knowledge={
            "CRM/OperatingRules": {
                "description": "Company CRM operating rules and source-of-truth decisions.",
                "seed_key": "rule",
                "columns": {
                    "rule": "str",
                    "details": "str",
                },
                "rows": [
                    {
                        "rule": "source_of_truth",
                        "details": "Live mutable CRM state belongs in Droid DataManager tables under Data/CRM.",
                    },
                    {
                        "rule": "bronze_cache",
                        "details": "Raw integration exports stay gitignored locally or in object storage, with normalized provenance rows in CRM tables.",
                    },
                    {
                        "rule": "recurring_syncs",
                        "details": "Recurring CRM work is represented as TaskScheduler tasks that call stable brain sync functions.",
                    },
                ],
            },
        },
        function_dir=_DIR / "functions",
        data_dir=_DIR / "data",
    )
