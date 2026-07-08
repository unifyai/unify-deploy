from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from unity_deploy.assistant_deployments.integrations.activation import (
    expand_integrations,
)
from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.scenarios.loader import (
    load_scenarios_from_integration,
)
from unity_deploy.assistant_deployments.scenarios import cli as scenario_cli
from unity_deploy.assistant_deployments.scenarios import runtime as scenario_runtime
from unity_deploy.assistant_deployments.scenarios.runtime import run_scenario_tick
from unity_deploy.assistant_deployments.scenarios.types import (
    IntegrationBinding,
    ScenarioSpec,
)


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


def test_loads_clientepsilon_mock_scenario_only_when_mock_packages_enabled():
    assert load_scenarios_from_integration("clientepsilon_homes_compliance_mock") == []

    scenarios = load_scenarios_from_integration(
        "clientepsilon_homes_compliance_mock",
        include_mock_packages=True,
    )

    assert len(scenarios) == 1
    assert scenarios[0].scenario_id == "clientepsilon_compliance_assurance_v1"
    assert scenarios[0].integration.mode == "mock"
    assert scenarios[0].tasks[0].execution_mode == "offline"
    assert scenarios[0].tasks[0].schedule.type == "cron"
    assert scenarios[0].tasks[0].activation.entrypoint_function == (
        "run_clientepsilon_compliance_sync_tick"
    )
    assert (
        scenarios[0].timeline[1].function == "run_clientepsilon_compliance_reasoning_tick"
    )


def test_expand_integrations_includes_mock_package_and_scenario():
    resolved = ResolvedAssistantDeployment(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        contacts=[],
        guidance_dirs=[],
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


def test_expand_integrations_includes_clientepsilon_mock_package_and_scenario():
    resolved = ResolvedAssistantDeployment(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        contacts=[],
        guidance_dirs=[],
        knowledge={},
        blacklist=[],
        secrets=[],
        integrations=["clientepsilon_homes_compliance_mock"],
    )

    expanded = expand_integrations(resolved)

    assert any(path.name == "functions" for path in expanded.function_dirs)
    assert len(expanded.scenarios) == 1
    assert (
        expanded.scenarios[0].integration.package_slug
        == "clientepsilon_homes_compliance_mock"
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
async def test_run_clientepsilon_scenario_tick_materializes_and_alerts():
    scenario = load_scenarios_from_integration(
        "clientepsilon_homes_compliance_mock",
        include_mock_packages=True,
    )[0]
    fake_dm = FakeDataManager()

    result = await run_scenario_tick(
        scenario,
        tick=0,
        data_manager=fake_dm,
        include_mock_packages=True,
    )

    contexts = [entry[0] for entry in fake_dm.ingests]
    assert "ClientEpsilonHomes/Demo/Compliance/Certificates" in contexts
    assert "ClientEpsilonHomes/Demo/Compliance/Alerts/Outbox" in contexts
    assert {alert["rule_id"] for alert in result.alerts} == {
        "certificate_enters_renewal_window",
        "missing_sharepoint_certificate",
    }


@pytest.mark.asyncio
async def test_run_clientepsilon_reasoning_tick_materializes_reasoned_tables(
    monkeypatch: pytest.MonkeyPatch,
):
    scenario = load_scenarios_from_integration(
        "clientepsilon_homes_compliance_mock",
        include_mock_packages=True,
    )[0]
    fake_dm = FakeDataManager()

    async def fake_invoke_connector(*args, **kwargs):
        return {
            "schema_version": "clientepsilon.compliance.snapshot.v1",
            "tables": {
                "certificates": [
                    {
                        "certificate_id": "CERT-GH-1001-GAS-1",
                        "property_id": "GH-1001",
                        "certificate_type": "gas",
                        "certificate_present": True,
                        "days_until_expiry": 45,
                        "renewal_status": "renewal_due",
                    },
                    {
                        "certificate_id": "CERT-GH-1002-ELECTRICAL-1",
                        "property_id": "GH-1002",
                        "certificate_type": "electrical",
                        "certificate_present": False,
                        "days_until_expiry": 9999,
                        "renewal_status": "missing_certificate",
                    },
                ],
                "reasoned_certificate_decisions": [
                    {
                        "decision_id": "DECISION-1",
                        "property_id": "GH-1001",
                        "certificate_type": "gas",
                        "renewal_status": "renewal_due",
                    },
                ],
                "email_delivery_results": [
                    {
                        "delivery_id": "EMAIL-1",
                        "status": "simulated",
                    },
                ],
            },
        }

    monkeypatch.setattr(scenario_runtime, "invoke_connector", fake_invoke_connector)

    result = await run_scenario_tick(
        scenario,
        tick=1,
        data_manager=fake_dm,
        include_mock_packages=True,
    )

    contexts = [entry[0] for entry in fake_dm.ingests]
    assert "ClientEpsilonHomes/Demo/Compliance/ReasonedCertificateDecisions" in contexts
    assert "ClientEpsilonHomes/Demo/Compliance/EmailDeliveryResults" in contexts
    assert {alert["rule_id"] for alert in result.alerts} == {
        "certificate_enters_renewal_window",
        "missing_sharepoint_certificate",
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
