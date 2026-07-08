"""Default Unify org deployment — company brain + scheduled job surface."""

from __future__ import annotations

from pathlib import Path

from unity_deploy.assistant_deployments.clients.unify_company._brain_operator import (
    brain_operator_assistant_id,
    brain_operator_tasks_enabled,
)
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec
from unity_deploy.assistant_deployments.scenarios.types import ScenarioActivation

_DIR = Path(__file__).resolve().parent


def _scenario_activations() -> list[ScenarioActivation]:
    assistant_id = brain_operator_assistant_id()
    if not assistant_id:
        return []
    return [
        ScenarioActivation(
            scenario_template="brain_jobs/brain_jobs_v0",
            assistant_id=assistant_id,
            scenario_id_override="unify_company_brain_jobs_v0",
            tasks_enabled=brain_operator_tasks_enabled(),
        ),
    ]


def get_deployment() -> DeploymentSpec:
    return DeploymentSpec(
        name="default",
        actor_config=ActorConfig(),
        scenarios=_scenario_activations(),
        guidance_dir=_DIR / "guidance",
        knowledge_dir=_DIR / "knowledge",
        function_dir=_DIR / "functions",
        data_dir=_DIR / "data",
    )
