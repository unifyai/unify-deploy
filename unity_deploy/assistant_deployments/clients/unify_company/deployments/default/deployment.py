"""Default Unify company-brain deployment seeds."""

from __future__ import annotations

from pathlib import Path

from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec

_DIR = Path(__file__).resolve().parent


def get_deployment() -> DeploymentSpec:
    return DeploymentSpec(
        name="default",
        actor_config=ActorConfig(),
        guidance_dir=_DIR / "guidance",
        secrets_dir=_DIR / "secrets",
        knowledge_dir=_DIR / "knowledge",
        function_dir=_DIR / "functions",
        data_dir=_DIR / "data",
    )
