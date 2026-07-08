from __future__ import annotations

import logging

from unity_deploy.assistant_deployments.clients import ClientDeploymentEntry
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentSpec,
    DeploymentTarget,
)
from unity_deploy import deployment_reconcile
from unity_deploy.deployment_reconcile import executor
from unity_deploy.scripts import reconcile_deployment


def _spec(name: str, console_config: dict | None = None) -> DeploymentSpec:
    return DeploymentSpec(
        name=name,
        actor_config=ActorConfig(guidelines=f"{name} guidelines"),
        console_config=console_config,
    )


def _registry() -> dict[str, ClientDeploymentEntry]:
    return {
        "client_a": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="101",
                        deployment="v1",
                    ),
                    DeploymentTarget(scope="org", scope_id="7", deployment="v2"),
                    DeploymentTarget(scope="default", deployment="v1"),
                ],
            ),
            specs={
                "v1": _spec("v1", console_config={"version": "1"}),
                "v2": _spec("v2"),
            },
            environment="staging",
        ),
    }


def _optional_registry() -> dict[str, ClientDeploymentEntry]:
    return {
        "unify_company": ClientDeploymentEntry(
            mapping=DeploymentMapping(
                targets=[
                    DeploymentTarget(
                        scope="assistant",
                        scope_id="2098",
                        deployment="default",
                        missing_ok=True,
                    ),
                ],
            ),
            specs={
                "default": _spec(
                    "default",
                    console_config={"version": "1"},
                ),
            },
            environment="staging",
        ),
    }


def test_build_deployment_target_plans_only_uses_assistant_targets():
    plans = deployment_reconcile.build_deployment_target_plans(
        environment="staging",
        registry=_registry(),
    )

    assert len(plans) == 1
    assert plans[0].target_key == "staging/client_a/v1/101"
    assert plans[0].runtime_summary["guidance_dir_count"] == 0
    assert plans[0].control_plane_revision
    assert plans[0].runtime_revision


def test_build_deployment_target_plans_carries_optional_target_flag():
    plans = deployment_reconcile.build_deployment_target_plans(
        environment="staging",
        registry=_optional_registry(),
    )

    assert len(plans) == 1
    assert plans[0].missing_ok is True
    assert plans[0].control_plane_operations[0].missing_ok is True


def test_build_deployment_work_items_splits_planes():
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("control-plane", "runtime"),
        registry=_registry(),
    )

    assert [item.plane for item in items] == ["control-plane", "runtime"]
    assert all(
        item.idempotency_key.startswith("staging/client_a/v1/101") for item in items
    )


def test_execute_work_items_dry_run_marks_planned():
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("control-plane",),
        registry=_registry(),
    )

    results = deployment_reconcile.execute_work_items(items, apply=False)

    assert [result.status for result in results] == ["planned"]


def test_runtime_apply_fails_when_assistant_identity_cannot_be_resolved(monkeypatch):
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("runtime",),
        registry=_registry(),
    )
    monkeypatch.setattr(
        executor,
        "_fetch_assistant_info",
        lambda assistant_id: (_ for _ in ()).throw(RuntimeError("missing assistant")),
    )

    result = deployment_reconcile.apply_work_item(items[0])

    assert result.status == "failed"
    assert "missing assistant" in result.error


def test_runtime_apply_skips_missing_optional_assistant(monkeypatch):
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("runtime",),
        registry=_optional_registry(),
    )
    monkeypatch.setattr(
        executor,
        "_fetch_assistant_info",
        lambda assistant_id: (_ for _ in ()).throw(
            executor.AssistantNotFoundError("missing assistant"),
        ),
    )

    result = deployment_reconcile.apply_work_item(items[0])

    assert result.status == "skipped-missing"
    assert "missing assistant" in result.message


def test_control_plane_apply_reports_missing_optional_assistant(monkeypatch):
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("control-plane",),
        registry=_optional_registry(),
    )
    from unity_deploy.deployment_reconcile import control_plane

    monkeypatch.setattr(
        control_plane,
        "apply_operations",
        lambda operations: [
            {
                "status": "skipped-missing",
                "assistant_id": "2098",
                "field": "console_config",
            },
        ],
    )

    result = deployment_reconcile.apply_work_item(items[0])

    assert result.status == "skipped-missing"
    assert result.message == "assistant target is missing"


def test_runtime_apply_requires_user_id_and_api_key(monkeypatch):
    items = deployment_reconcile.build_deployment_work_items(
        environment="staging",
        planes=("runtime",),
        registry=_registry(),
    )
    monkeypatch.setattr(
        executor,
        "_fetch_assistant_info",
        lambda assistant_id: {"user_id": "user-1"},
    )

    result = deployment_reconcile.apply_work_item(items[0])

    assert result.status == "failed"
    assert "api_key" in result.error


def test_parse_planes_rejects_unknown_plane():
    try:
        deployment_reconcile.parse_planes("control-plane,unknown")
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("parse_planes should reject unknown planes")


def test_unified_cli_dry_run(monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin")
    monkeypatch.setattr(
        deployment_reconcile,
        "build_deployment_work_items",
        lambda **kwargs: [],
    )

    with caplog.at_level(logging.INFO):
        exit_code = reconcile_deployment.main(
            [
                "--environment",
                "staging",
                "--planes",
                "control-plane",
                "--dry-run",
            ],
        )

    assert exit_code == 0
    assert "No deployment reconciliation work planned" in caplog.text


def test_unified_cli_runtime_apply_does_not_require_global_unify_key(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin")
    monkeypatch.delenv("UNIFY_KEY", raising=False)
    monkeypatch.setattr(
        deployment_reconcile,
        "build_deployment_work_items",
        lambda **kwargs: [],
    )

    with caplog.at_level(logging.INFO):
        exit_code = reconcile_deployment.main(
            [
                "--environment",
                "staging",
                "--planes",
                "runtime",
                "--apply",
            ],
        )

    assert exit_code == 0
    assert "No deployment reconciliation work planned" in caplog.text
