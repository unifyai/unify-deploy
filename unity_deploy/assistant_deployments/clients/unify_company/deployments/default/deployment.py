"""Default Unify company-brain deployment seeds."""

from __future__ import annotations

from pathlib import Path

from unify.secret_manager.types import Secret
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec

_DIR = Path(__file__).resolve().parent


def get_deployment() -> DeploymentSpec:
    return DeploymentSpec(
        name="default",
        actor_config=ActorConfig(),
        guidance_dir=_DIR / "guidance",
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
                        "details": "Live mutable CRM state belongs in Unity DataManager tables under Data/CRM.",
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
