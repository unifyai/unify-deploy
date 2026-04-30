from __future__ import annotations

import logging

from unity_deploy.control_plane import reconcile
from unity_deploy.customization.clients import ClientDeploymentEntry
from unity_deploy.customization.configs.types.actor_config import ActorConfig
from unity_deploy.customization.deployment_types import (
    DeploymentMapping,
    DeploymentSpec,
    DeploymentTarget,
)
from unity_deploy.scripts import reconcile_control_plane


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


def test_cli_returns_error_when_environment_variables_missing(monkeypatch, caplog):
    monkeypatch.delenv("ORCHESTRA_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_ADMIN_KEY", raising=False)

    with caplog.at_level(logging.ERROR):
        exit_code = reconcile_control_plane.main(
            ["--environment", "staging", "--dry-run"],
        )

    assert exit_code == 2
    assert "Missing required environment variable" in caplog.text


def test_cli_dry_run_does_not_apply(monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin")

    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="console_config",
        action="upsert",
        path="/admin/assistant/1851",
        payload={"console_config": {"version": "1"}},
    )
    monkeypatch.setattr(
        reconcile,
        "build_control_plane_plan",
        lambda **kwargs: [operation],
    )

    def fail_apply(operations):
        raise AssertionError("dry-run should not apply")

    monkeypatch.setattr(reconcile, "apply_operations", fail_apply)

    with caplog.at_level(logging.INFO):
        exit_code = reconcile_control_plane.main(
            ["--environment", "staging", "--client", "client_alpha", "--dry-run"],
        )

    assert exit_code == 0
    assert "Dry-run complete" in caplog.text


def test_cli_apply_runs_planned_operations(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin")

    operation = reconcile.ReconcileOperation(
        client_name="client_alpha",
        assistant_id="1851",
        deployment="v2",
        field="console_config",
        action="upsert",
        path="/admin/assistant/1851",
        payload={"console_config": {"version": "1"}},
    )
    applied: list[reconcile.ReconcileOperation] = []
    monkeypatch.setattr(
        reconcile,
        "build_control_plane_plan",
        lambda **kwargs: [operation],
    )
    monkeypatch.setattr(
        reconcile,
        "apply_operations",
        lambda operations: applied.extend(operations) or [{"ok": True}],
    )

    exit_code = reconcile_control_plane.main(
        ["--environment", "staging", "--assistant-id", "1851", "--apply"],
    )

    assert exit_code == 0
    assert applied == [operation]


def test_cli_rejects_mismatched_environment(monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin")

    with caplog.at_level(logging.ERROR):
        exit_code = reconcile_control_plane.main(
            ["--environment", "staging", "--dry-run"],
        )

    assert exit_code == 2
    assert "does not match" in caplog.text
