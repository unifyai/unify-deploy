from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from unity_deploy.customization.integrations.activation import expand_integrations
from unity_deploy.customization.clients import ResolvedCustomization
from unity_deploy.customization.configs.types.actor_config import ActorConfig
from unity_deploy.customization.scenarios.loader import load_scenarios_from_integration
from unity_deploy.customization.scenarios import cli as scenario_cli
from unity_deploy.customization.scenarios import runtime as scenario_runtime
from unity_deploy.customization.scenarios.runtime import run_scenario_tick
from unity_deploy.customization.scenarios.types import IntegrationBinding, ScenarioSpec


class FakeDataManager:
    def __init__(self):
        self.ingests = []

    def ingest(self, context, rows=None, **kwargs):
        self.ingests.append((context, rows or [], kwargs))
        return {"status": "ok"}


def test_mock_binding_requires_mock_slug():
    with pytest.raises(ValueError, match="ending in '_mock'"):
        IntegrationBinding(
            package_slug="client_alpha_repairs",
            mode="mock",
            schema_version="v1",
        )


def test_loads_client_alpha_mock_scenario_only_when_mock_packages_enabled():
    assert load_scenarios_from_integration("client_alpha_repairs_mock") == []

    scenarios = load_scenarios_from_integration(
        "client_alpha_repairs_mock",
        include_mock_packages=True,
    )

    assert len(scenarios) == 1
    assert scenarios[0].scenario_id == "client_alpha_repairs_alerts_v1"
    assert scenarios[0].integration.mode == "mock"
    assert scenarios[0].tasks[0].execution_mode == "offline"
    assert scenarios[0].tasks[0].schedule.type == "interval"
    assert scenarios[0].tasks[0].target.assistant_id == "1851"
    assert scenarios[0].tasks[0].activation.entrypoint_function == (
        "run_client_alpha_repairs_monitoring_tick"
    )


def test_expand_integrations_includes_mock_package_and_scenario():
    resolved = ResolvedCustomization(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        contacts=[],
        guidance=[],
        knowledge={},
        blacklist=[],
        secrets=[],
        integrations=["client_alpha_repairs_mock"],
    )

    expanded = expand_integrations(resolved)

    assert any(path.name == "functions" for path in expanded.function_dirs)
    assert len(expanded.scenarios) == 1
    assert (
        expanded.scenarios[0].integration.package_slug == "client_alpha_repairs_mock"
    )


@pytest.mark.asyncio
async def test_run_scenario_tick_materializes_and_alerts():
    scenario = load_scenarios_from_integration(
        "client_alpha_repairs_mock",
        include_mock_packages=True,
    )[0]
    fake_dm = FakeDataManager()

    result = await run_scenario_tick(
        scenario,
        tick=3,
        data_manager=fake_dm,
        include_mock_packages=True,
    )

    contexts = [entry[0] for entry in fake_dm.ingests]
    assert "ClientAlpha/v2/Pilot/Repairs/Snapshot" in contexts
    assert "ClientAlpha/v2/Pilot/Alerts/Outbox" in contexts
    assert {alert["rule_id"] for alert in result.alerts} == {
        "no_access_rate_spike",
        "emergency_backlog_spike",
    }


@pytest.mark.asyncio
async def test_run_scenario_tick_activates_context_before_manager_writes(monkeypatch):
    scenario = load_scenarios_from_integration(
        "client_alpha_repairs_mock",
        include_mock_packages=True,
    )[0]
    events: list[tuple[str, str | None, str | None]] = []

    @asynccontextmanager
    async def fake_context(identity, **kwargs):
        events.append(("activate", identity.user_id, identity.assistant_id))
        yield
        events.append(("restore", None, None))

    def fake_materialize(spec, snapshot, *, data_manager=None):
        events.append(("materialize", None, None))
        return ["ctx"]

    def fake_outbox(spec, alerts, *, data_manager=None):
        events.append(("outbox", None, None))
        return ["outbox"]

    monkeypatch.setattr(scenario_runtime, "activate_scenario_context", fake_context)
    monkeypatch.setattr(scenario_runtime, "materialize_snapshot", fake_materialize)
    monkeypatch.setattr(scenario_runtime, "write_alert_outbox", fake_outbox)

    result = await scenario_runtime.run_scenario_tick(
        scenario,
        tick=3,
        user_id="user-123",
        assistant_id="1851",
        include_mock_packages=True,
    )

    assert result.materialized_contexts == ["ctx"]
    assert events == [
        ("activate", "user-123", "1851"),
        ("materialize", None, None),
        ("outbox", None, None),
        ("restore", None, None),
    ]


@pytest.mark.asyncio
async def test_run_scenario_tick_requires_identity_for_default_manager_writes():
    scenario = load_scenarios_from_integration(
        "client_alpha_repairs_mock",
        include_mock_packages=True,
    )[0]

    with pytest.raises(ValueError, match="user_id and assistant_id"):
        await scenario_runtime.run_scenario_tick(
            scenario,
            tick=0,
            include_mock_packages=True,
        )


def test_cli_no_write_scenario_run_does_not_require_identity():
    args = scenario_cli._build_parser().parse_args(
        [
            "once",
            "--integration",
            "client_alpha_repairs_mock",
            "--include-mock-packages",
            "--no-materialize",
            "--no-outbox",
        ],
    )

    scenario_cli._validate_identity(args)


def test_cli_mutating_scenario_run_requires_identity():
    args = scenario_cli._build_parser().parse_args(
        [
            "once",
            "--integration",
            "client_alpha_repairs_mock",
            "--include-mock-packages",
        ],
    )

    with pytest.raises(SystemExit, match="--user-id and --assistant-id"):
        scenario_cli._validate_identity(args)


def test_scenario_rejects_unknown_monitor_route():
    raw = {
        "scenario_id": "bad",
        "name": "Bad",
        "description": "Bad route",
        "client": "client_alpha",
        "deployment": "v2",
        "integration": {
            "package_slug": "x_mock",
            "mode": "mock",
            "required_capabilities": ["feed"],
            "schema_version": "v1",
        },
        "data_targets": [{"table": "rows", "context": "Ctx"}],
        "timeline": [{"tick": 0, "capability": "feed", "function": "fetch"}],
        "monitors": [
            {
                "id": "m",
                "table": "rows",
                "field": "value",
                "operator": ">",
                "threshold": 1,
                "route": "missing",
                "message_template": "bad",
            },
        ],
    }
    with pytest.raises(ValueError, match="unknown route"):
        ScenarioSpec.model_validate(raw)
