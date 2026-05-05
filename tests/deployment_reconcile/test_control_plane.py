from __future__ import annotations

import pytest

from unity_deploy.deployment_reconcile import control_plane as reconcile
from unity_deploy.assistant_deployments.clients import ClientDeploymentEntry
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentSpec,
    DeploymentTarget,
)


def _spec(name: str, console_config: dict | None = None) -> DeploymentSpec:
    return DeploymentSpec(
        name=name,
        actor_config=ActorConfig(guidelines=f"{name} guidelines"),
        console_config=console_config,
    )


def _registry() -> dict[str, ClientDeploymentEntry]:
    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
    }
    return {
        "client_alpha": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="1851",
                        deployment="v2",
                    ),
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="1516",
                        deployment="v1",
                    ),
                    DeploymentTarget(scope="org", scope_id="7", deployment="v1"),
                ],
            ),
            specs={
                "v1": _spec("v1"),
                "v2": _spec("v2", console_config=console_config),
            },
            environment="staging",
        ),
        "other_client": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="999",
                        deployment="v1",
                    ),
                ],
            ),
            specs={"v1": _spec("v1", console_config={"version": "1"})},
            environment="staging",
        ),
    }


def test_build_control_plane_plan_upserts_and_clears_assistant_targets():
    operations = reconcile.build_control_plane_plan(
        environment="staging",
        client="client_alpha",
        registry=_registry(),
    )

    assert [op.assistant_id for op in operations] == ["1851", "1516"]
    assert operations[0].action == "upsert"
    assert operations[0].path == "/admin/assistant/1851"
    assert operations[0].payload == {
        "console_config": {
            "version": "1",
            "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        },
    }
    assert operations[1].action == "clear"
    assert operations[1].path == "/admin/assistant/1516"
    assert operations[1].payload == {"console_config": None}


def test_build_control_plane_plan_filters_by_assistant_id():
    operations = reconcile.build_control_plane_plan(
        environment="staging",
        assistant_id="1851",
        registry=_registry(),
    )

    assert len(operations) == 1
    assert operations[0].assistant_id == "1851"


def test_build_control_plane_plan_filters_by_client():
    operations = reconcile.build_control_plane_plan(
        environment="staging",
        client="other_client",
        registry=_registry(),
    )

    assert len(operations) == 1
    assert operations[0].client_name == "other_client"
    assert operations[0].assistant_id == "999"


def test_build_control_plane_plan_projects_scenario_to_generic_task_activation():
    registry = {
        "client_alpha": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="1851",
                        deployment="v2",
                    ),
                ],
            ),
            specs={
                "v2": DeploymentSpec(
                    name="v2",
                    actor_config=ActorConfig(guidelines="v2 guidelines"),
                    integrations=["client_alpha_repairs_mock"],
                ),
            },
            environment="staging",
        ),
    }

    operations = reconcile.build_control_plane_plan(
        environment="staging",
        client="client_alpha",
        registry=registry,
    )

    task_ops = [op for op in operations if op.field == "task_activation"]
    assert task_ops == []


def test_build_control_plane_plan_keeps_clientepsilon_disabled_schedule_private():
    registry = {
        "clientepsilon_homes": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="2999",
                        deployment="demo",
                    ),
                ],
            ),
            specs={
                "demo": DeploymentSpec(
                    name="demo",
                    actor_config=ActorConfig(guidelines="demo guidelines"),
                    integrations=["clientepsilon_homes_compliance_mock"],
                ),
            },
            environment="staging",
        ),
    }

    operations = reconcile.build_control_plane_plan(
        environment="staging",
        client="clientepsilon_homes",
        registry=registry,
    )

    task_ops = [op for op in operations if op.field == "task_activation"]
    assert task_ops == []


def test_build_control_plane_plan_skips_environment_mismatch():
    operations = reconcile.build_control_plane_plan(
        environment="production",
        registry=_registry(),
    )

    assert operations == []


def test_apply_operations_patches_each_assistant(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_patch_json(path, body):
        captured.append((path, body))
        return {"status": "ok", "path": path}

    from unity_deploy.utils import orchestra_client

    monkeypatch.setattr(orchestra_client, "patch_json", fake_patch_json)
    operations = reconcile.build_control_plane_plan(
        environment="staging",
        client="client_alpha",
        registry=_registry(),
    )

    responses = reconcile.apply_operations(operations)

    assert len(responses) == 2
    assert captured == [
        (operations[0].path, operations[0].payload),
        (operations[1].path, operations[1].payload),
    ]


def test_apply_operations_posts_communication_operations(monkeypatch):
    captured: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        reconcile,
        "_post_communication_json",
        lambda path, payload: captured.append((path, payload)) or {"ok": True},
    )
    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="task_activation",
        action="upsert",
        path="/infra/task-activation/upsert",
        payload={"task_id": 1},
        service="communication",
        method="post",
    )

    responses = reconcile.apply_operations([operation])

    assert responses == [{"ok": True}]
    assert captured == [
        ("/infra/task-activation/upsert", operation.payload),
    ]


def test_apply_operations_rejects_unresolved_task_activation():
    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="task_activation",
        action="unresolved",
        path="/infra/task-activation/upsert",
        payload={"unresolved_reason": "missing task ids"},
        service="communication",
        method="post",
    )

    with pytest.raises(RuntimeError, match="missing task ids"):
        reconcile.apply_operations([operation])
