"""CLI for running deployment scenario ticks locally or from K8s Jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from droid_deploy.assistant_deployments.scenarios.loader import (
    load_scenario,
    load_scenarios_from_integration,
)
from droid_deploy.assistant_deployments.scenarios.runtime import (
    evaluate_monitors,
    run_scenario_tick,
)

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run droid-deploy scenario workflows")
    parser.add_argument("command", choices=["once", "poll", "replay", "evaluate"])
    parser.add_argument("--scenario-file", default=None)
    parser.add_argument("--integration", default=None)
    parser.add_argument("--scenario-id", default=None)
    parser.add_argument("--include-mock-packages", action="store_true")
    parser.add_argument("--tick", type=int, default=0)
    parser.add_argument("--start-tick", type=int, default=0)
    parser.add_argument("--end-tick", type=int, default=0)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--assistant-id", default=None)
    parser.add_argument("--project", default="Assistants")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--max-ticks", type=int, default=1)
    parser.add_argument("--snapshot-json", default=None)
    parser.add_argument("--no-materialize", action="store_true")
    parser.add_argument("--no-outbox", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def _load_selected_scenario(args: argparse.Namespace):
    if args.scenario_file:
        return load_scenario(Path(args.scenario_file))
    if not args.integration:
        raise SystemExit("Provide --scenario-file or --integration")
    scenarios = load_scenarios_from_integration(
        args.integration,
        include_mock_packages=args.include_mock_packages,
    )
    if not scenarios:
        raise SystemExit(f"No scenarios found for integration {args.integration!r}")
    if args.scenario_id:
        for scenario in scenarios:
            if scenario.scenario_id == args.scenario_id:
                return scenario
        raise SystemExit(f"Scenario {args.scenario_id!r} not found")
    if len(scenarios) > 1:
        raise SystemExit("Multiple scenarios found; pass --scenario-id")
    return scenarios[0]


def _requires_identity(args: argparse.Namespace) -> bool:
    return args.command in {"once", "poll", "replay"} and (
        not args.no_materialize or not args.no_outbox
    )


def _validate_identity(args: argparse.Namespace) -> None:
    if _requires_identity(args) and (not args.user_id or not args.assistant_id):
        raise SystemExit(
            "Mutating scenario commands require --user-id and --assistant-id "
            "unless both --no-materialize and --no-outbox are set.",
        )


def _print_result(result) -> None:
    print(
        json.dumps(
            {
                "scenario_id": result.scenario_id,
                "tick": result.tick,
                "materialized_contexts": result.materialized_contexts,
                "alerts": result.alerts,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
    )


async def _run(args: argparse.Namespace) -> int:
    scenario = _load_selected_scenario(args)
    _validate_identity(args)
    if args.command == "evaluate":
        if not args.snapshot_json:
            raise SystemExit("evaluate requires --snapshot-json")
        snapshot = json.loads(Path(args.snapshot_json).read_text())
        alerts = evaluate_monitors(scenario, snapshot, tick=args.tick)
        print(json.dumps({"alerts": alerts}, indent=2, sort_keys=True, default=str))
        return 0

    if args.command == "once":
        result = await run_scenario_tick(
            scenario,
            tick=args.tick,
            user_id=args.user_id,
            assistant_id=args.assistant_id,
            project_name=args.project,
            api_key=args.api_key,
            include_mock_packages=args.include_mock_packages,
            materialize=not args.no_materialize,
            write_outbox=not args.no_outbox,
        )
        _print_result(result)
        return 0

    if args.command == "poll":
        tick = args.tick
        for index in range(args.max_ticks):
            result = await run_scenario_tick(
                scenario,
                tick=tick,
                user_id=args.user_id,
                assistant_id=args.assistant_id,
                project_name=args.project,
                api_key=args.api_key,
                include_mock_packages=args.include_mock_packages,
                materialize=not args.no_materialize,
                write_outbox=not args.no_outbox,
            )
            _print_result(result)
            tick += 1
            if index + 1 < args.max_ticks:
                await asyncio.sleep(args.interval_seconds)
        return 0

    for tick in range(args.start_tick, args.end_tick + 1):
        result = await run_scenario_tick(
            scenario,
            tick=tick,
            user_id=args.user_id,
            assistant_id=args.assistant_id,
            project_name=args.project,
            api_key=args.api_key,
            include_mock_packages=args.include_mock_packages,
            materialize=not args.no_materialize,
            write_outbox=not args.no_outbox,
        )
        _print_result(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
