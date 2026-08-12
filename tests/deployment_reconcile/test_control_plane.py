from __future__ import annotations

import pytest

from unify_deploy.deployment_reconcile import control_plane as reconcile
from unify_deploy.assistant_deployments.clients import ClientDeploymentEntry
from unify_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unify_deploy.assistant_deployments.deployment_types import (
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


def test_build_control_plane_plan_does_not_emit_task_execution_ops():
    """Integrations do not project schedules into task activations."""
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

    assert all(op.field != "task_execution" for op in operations)
    assert all(op.field == "console_config" for op in operations)


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

    from unify_deploy.utils import orchestra_client

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
        field="task_execution",
        action="upsert",
        path="/infra/task-execution/upsert",
        payload={"task_id": 1},
        service="communication",
        method="post",
    )

    responses = reconcile.apply_operations([operation])

    assert responses == [{"ok": True}]
    assert captured == [
        ("/infra/task-execution/upsert", operation.payload),
    ]


def test_apply_operations_rejects_unresolved_task_execution():
    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="task_execution",
        action="unresolved",
        path="/infra/task-execution/upsert",
        payload={"unresolved_reason": "missing task ids"},
        service="communication",
        method="post",
    )

    with pytest.raises(RuntimeError, match="missing task ids"):
        reconcile.apply_operations([operation])


def test_apply_operations_defers_generic_task_execution(monkeypatch):
    """Deferred control-plane ops are recorded locally and not transmitted."""

    def _boom(*args, **kwargs):
        raise AssertionError("deferred operations must not be transmitted")

    monkeypatch.setattr(reconcile, "_post_communication_json", _boom)
    from unify_deploy.utils import orchestra_client

    monkeypatch.setattr(orchestra_client, "patch_json", _boom)

    operation = reconcile.ReconcileOperation(
        client_name="unify_company",
        assistant_id="2098",
        deployment="v0",
        field="task_execution",
        action="deferred",
        path="/infra/task-execution/upsert",
        payload={"deferred_reason": "activation ids not seeded yet"},
        service="communication",
        method="post",
    )

    responses = reconcile.apply_operations([operation])

    assert responses == [
        {
            "status": "deferred",
            "assistant_id": "2098",
            "field": "task_execution",
            "reason": "activation ids not seeded yet",
        },
    ]


def test_apply_operations_skips_missing_optional_assistant(monkeypatch):
    from unify_deploy.utils import orchestra_client

    def missing_assistant(*args, **kwargs):
        raise orchestra_client.OrchestraClientError(404, "not found")

    def communication_not_called(*args, **kwargs):
        raise AssertionError("missing optional assistant must suppress later writes")

    monkeypatch.setattr(orchestra_client, "patch_json", missing_assistant)
    monkeypatch.setattr(reconcile, "_post_communication_json", communication_not_called)

    operations = [
        reconcile.ReconcileOperation(
            client_name="unify_company",
            assistant_id="2098",
            deployment="default",
            field="console_config",
            action="clear",
            path="/admin/assistant/2098",
            payload={"console_config": None},
            missing_ok=True,
        ),
        reconcile.ReconcileOperation(
            client_name="unify_company",
            assistant_id="2098",
            deployment="default",
            field="task_execution",
            action="upsert",
            path="/infra/task-execution/upsert",
            payload={"task_id": 1},
            service="communication",
            method="post",
            missing_ok=True,
        ),
    ]

    responses = reconcile.apply_operations(operations)

    assert responses == [
        {
            "status": "skipped-missing",
            "assistant_id": "2098",
            "field": "console_config",
            "reason": "assistant target 2098 not found",
        },
        {
            "status": "skipped-missing",
            "assistant_id": "2098",
            "field": "task_execution",
            "reason": "assistant target is missing",
        },
    ]


def test_apply_operations_keeps_missing_required_assistant_fatal(monkeypatch):
    from unify_deploy.utils import orchestra_client

    def missing_assistant(*args, **kwargs):
        raise orchestra_client.OrchestraClientError(404, "not found")

    monkeypatch.setattr(orchestra_client, "patch_json", missing_assistant)

    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="console_config",
        action="upsert",
        path="/admin/assistant/1851",
        payload={"console_config": {"version": "1"}},
    )

    with pytest.raises(RuntimeError) as excinfo:
        reconcile.apply_operations([operation])

    message = str(excinfo.value)
    assert "1851" in message
    assert "client_alpha" in message
    assert "routing_manifest.yaml" in message
    assert "substitution" in message
    assert isinstance(excinfo.value.__cause__, orchestra_client.OrchestraClientError)
