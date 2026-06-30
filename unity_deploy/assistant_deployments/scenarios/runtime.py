"""Runtime helpers for scenario ticks, monitor evaluation, and alert outboxes."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from unity_deploy.assistant_deployments.scenarios.context import (
    ScenarioIdentity,
    activate_scenario_context,
)
from unity_deploy.assistant_deployments.scenarios.loader import find_integration_root
from unity_deploy.assistant_deployments.scenarios.types import (
    AlertRoute,
    MonitorRule,
    ScenarioSpec,
    TimelineEvent,
)


@dataclass
class ScenarioRunResult:
    """Structured result for one scenario tick."""

    scenario_id: str
    tick: int
    snapshot: dict[str, Any]
    materialized_contexts: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    raise ValueError(f"Unsupported operator: {operator}")


def _render_message(template: str, *, row: dict[str, Any], rule: MonitorRule) -> str:
    payload = {**row, "rule_id": rule.id, "threshold": rule.threshold}
    try:
        return template.format(**payload)
    except KeyError:
        return template


def _route_for(spec: ScenarioSpec, route_id: str) -> AlertRoute:
    for route in spec.alert_routes:
        if route.id == route_id:
            return route
    raise KeyError(f"Unknown alert route: {route_id}")


def select_timeline_event(spec: ScenarioSpec, tick: int) -> TimelineEvent:
    """Return the latest timeline event at or before *tick*."""
    eligible = [event for event in spec.timeline if event.tick <= tick]
    if not eligible:
        raise ValueError(f"No timeline event available for tick {tick}")
    return sorted(eligible, key=lambda event: event.tick)[-1]


def evaluate_monitors(
    spec: ScenarioSpec,
    snapshot: dict[str, Any],
    *,
    tick: int,
) -> list[dict[str, Any]]:
    """Evaluate monitor rules against a normalized connector snapshot."""
    tables = snapshot.get("tables", {})
    alerts: list[dict[str, Any]] = []
    evaluated_at = datetime.now(timezone.utc).isoformat()

    for rule in spec.monitors:
        rows = tables.get(rule.table, [])
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows or []:
            if rule.field not in row:
                continue
            value = row[rule.field]
            try:
                matched = _compare(value, rule.operator, rule.threshold)
            except TypeError:
                continue
            if not matched:
                continue

            route = _route_for(spec, rule.route)
            alerts.append(
                {
                    "scenario_id": spec.scenario_id,
                    "tick": tick,
                    "rule_id": rule.id,
                    "severity": rule.severity,
                    "route_id": route.id,
                    "channel": route.channel,
                    "contact_ref": route.contact_ref,
                    "message": _render_message(
                        rule.message_template,
                        row=row,
                        rule=rule,
                    ),
                    "metric_field": rule.field,
                    "metric_value": value,
                    "threshold": rule.threshold,
                    "evaluated_at": evaluated_at,
                    "status": (
                        "pending"
                        if route.channel != "simulated_outbox"
                        else "simulated"
                    ),
                },
            )
    return alerts


def materialize_snapshot(
    spec: ScenarioSpec,
    snapshot: dict[str, Any],
    *,
    data_manager: Any | None = None,
) -> list[str]:
    """Write normalized snapshot tables to configured DataManager contexts."""
    if data_manager is None:
        from unify.manager_registry import ManagerRegistry

        data_manager = ManagerRegistry.get_data_manager()

    contexts: list[str] = []
    tables = snapshot.get("tables", {})
    for target in spec.data_targets:
        rows = tables.get(target.table, [])
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            continue
        if target.unique_key:
            keys = (
                target.unique_key
                if isinstance(target.unique_key, list)
                else [target.unique_key]
            )
            unique_keys = {k: "str" for k in keys}
        else:
            unique_keys = None
        data_manager.ingest(
            target.context,
            rows=list(rows),
            description=target.description or None,
            fields=target.fields or None,
            unique_keys=unique_keys,
            infer_untyped_fields=True,
        )
        contexts.append(target.context)
    return contexts


def write_alert_outbox(
    spec: ScenarioSpec,
    alerts: list[dict[str, Any]],
    *,
    data_manager: Any | None = None,
) -> list[str]:
    """Persist simulated alerts to DataManager outbox contexts."""
    if not alerts:
        return []
    if data_manager is None:
        from unify.manager_registry import ManagerRegistry

        data_manager = ManagerRegistry.get_data_manager()

    contexts: list[str] = []
    for route in spec.alert_routes:
        if route.channel != "simulated_outbox" or not route.outbox_context:
            continue
        route_alerts = [a for a in alerts if a["route_id"] == route.id]
        if not route_alerts:
            continue
        rows = [
            {
                **alert,
                "payload_json": json.dumps(alert, sort_keys=True, default=str),
            }
            for alert in route_alerts
        ]
        data_manager.ingest(
            route.outbox_context,
            rows=rows,
            description="Simulated scenario alert outbox",
            infer_untyped_fields=True,
        )
        contexts.append(route.outbox_context)
    return contexts


def _load_function(package_root: Path, function_name: str) -> Callable[..., Any]:
    functions_dir = package_root / "functions"
    for py_file in sorted(functions_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"_scenario_{package_root.name}_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, function_name, None)
        if callable(fn):
            return fn
    raise AttributeError(f"Function '{function_name}' not found in {functions_dir}")


async def invoke_connector(
    spec: ScenarioSpec,
    event: TimelineEvent,
    *,
    integration_root: Path | None = None,
    include_mock_packages: bool = False,
) -> dict[str, Any]:
    """Invoke the integration function for one timeline event."""
    root = integration_root or find_integration_root(
        spec.integration.package_slug,
        include_mock_packages=include_mock_packages,
    )
    if root is None:
        raise FileNotFoundError(
            f"Integration package not found: {spec.integration.package_slug}",
        )
    fn = _load_function(root, event.function)
    kwargs = dict(event.args)
    kwargs.setdefault("tick", event.tick)
    kwargs.setdefault("scenario_id", spec.scenario_id)
    kwargs.setdefault("schema_version", spec.integration.schema_version)
    result = fn(**kwargs)
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        raise TypeError(
            f"Connector '{event.function}' returned {type(result).__name__}",
        )
    if result.get("schema_version") != spec.integration.schema_version:
        raise ValueError(
            "Connector schema_version mismatch: "
            f"expected {spec.integration.schema_version}, got {result.get('schema_version')}",
        )
    return result


async def run_scenario_tick(
    spec: ScenarioSpec,
    *,
    tick: int,
    data_manager: Any | None = None,
    user_id: str | None = None,
    assistant_id: str | None = None,
    project_name: str = "Assistants",
    api_key: str | None = None,
    integration_root: Path | None = None,
    include_mock_packages: bool = False,
    materialize: bool = True,
    write_outbox: bool = True,
) -> ScenarioRunResult:
    """Run connector, DataManager materialization, monitor evaluation, and outbox."""
    event = select_timeline_event(spec, tick)
    snapshot = await invoke_connector(
        spec,
        event,
        integration_root=integration_root,
        include_mock_packages=include_mock_packages,
    )
    materialized = []
    alerts = evaluate_monitors(spec, snapshot, tick=tick)

    if data_manager is None and (materialize or write_outbox):
        if not user_id or not assistant_id:
            raise ValueError(
                "user_id and assistant_id are required for mutating scenario runs",
            )
        from unify.data_manager.data_manager import DataManager

        identity = ScenarioIdentity(
            user_id=user_id,
            assistant_id=assistant_id,
            project_name=project_name,
        )
        async with activate_scenario_context(
            identity,
            managers=[DataManager],
            api_key=api_key,
        ):
            if materialize:
                materialized = materialize_snapshot(spec, snapshot)
            if write_outbox:
                write_alert_outbox(spec, alerts)
    else:
        if materialize:
            materialized = materialize_snapshot(
                spec,
                snapshot,
                data_manager=data_manager,
            )
        if write_outbox:
            write_alert_outbox(spec, alerts, data_manager=data_manager)
    return ScenarioRunResult(
        scenario_id=spec.scenario_id,
        tick=tick,
        snapshot=snapshot,
        materialized_contexts=materialized,
        alerts=alerts,
    )
