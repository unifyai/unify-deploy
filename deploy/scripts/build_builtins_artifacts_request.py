#!/usr/bin/env python3
"""Build a Builtins artifacts seed request from a provider manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

SEED_OWNER = "public-builtins"
SYNC_PASSTHROUGH_FIELDS = (
    "tool_limit_per_app",
    "component_limit_per_app",
    "include_all_managed_apps",
    "include_all_apps",
    "create_auth_configs",
    "sync_tools",
    "prune_unlisted_apps",
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return (
            json.loads(file.read().decode("utf-8"))
            if path.suffix == ".json"
            else tomllib.load(file)
        )


def build_request(
    manifest: dict[str, Any],
    *,
    environment: str,
    workers: int,
    batch_size: int,
    backend_id: str | None = None,
) -> dict[str, Any]:
    providers = manifest.get("providers") or {}
    sync_plans = []
    for provider_id, config in sorted(providers.items()):
        sync = (config or {}).get("sync")
        if (
            (backend_id is None or provider_id == backend_id)
            and (config or {}).get("status") == "enabled"
            and sync
        ):
            sync_plans.append((str(provider_id), config, sync))
    if len(sync_plans) != 1:
        raise ValueError(
            f"expected exactly one enabled provider sync, got {len(sync_plans)}",
        )

    provider_id, config, sync = sync_plans[0]
    mode = sync.get("mode", "partial")
    if mode not in {"partial", "full"}:
        raise ValueError(f"{provider_id}: sync.mode must be partial or full")

    backend_payload = {
        "backend_id": provider_id,
        "kind": config.get("kind") or provider_id,
        "environment": environment,
        "display_name": config.get("display_name") or provider_id.title(),
        "status": config.get("status", "disabled"),
        "allowed_orgs_or_tenants": config.get("allowed_orgs_or_tenants") or [],
        "default_priority": int(config.get("default_priority", 100)),
        "config_json": config.get("config_json") or {},
    }
    sync_payload: dict[str, Any] = {
        "backend_id": provider_id,
        "app_slugs": [] if mode == "full" else list(sync.get("app_slugs") or []),
        "sync_mode": mode,
    }
    for field in SYNC_PASSTHROUGH_FIELDS:
        if field in sync:
            sync_payload[field] = sync[field]
    if mode == "full":
        if "include_all_managed_apps" in sync_payload:
            sync_payload["include_all_managed_apps"] = True
        if "include_all_apps" in sync_payload:
            sync_payload["include_all_apps"] = True

    desired_config = {
        "schema_version": manifest.get("schema_version", 1),
        "environment": environment,
        "seed_owner": SEED_OWNER,
        "artifact_kind": "integrations",
        "backend": backend_payload,
        "sync": {**sync_payload, "mode": mode},
    }
    desired_hash = hashlib.sha256(_json_dumps(desired_config).encode()).hexdigest()
    sync_payload["cache_version"] = (
        f"{SEED_OWNER}-{environment}-{provider_id}-{desired_hash[:12]}"
    )
    return {
        "artifact_kind": "integrations",
        "backend_id": provider_id,
        "environment": environment,
        "desired_hash": desired_hash,
        "desired_config": desired_config,
        "cache_version": sync_payload["cache_version"],
        "mode": "all",
        "app_slugs": list(sync_payload.get("app_slugs") or []),
        "prune_unlisted_apps": bool(sync_payload.get("prune_unlisted_apps", False)),
        "sync_payload": sync_payload,
        "batch_size": int(batch_size),
        "workers": int(workers),
        "run_id": str(uuid.uuid4()),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--backend-id", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = build_request(
        _load_manifest(Path(args.manifest)),
        environment=args.environment,
        workers=args.workers,
        batch_size=args.batch_size,
        backend_id=args.backend_id or None,
    )
    print(_json_dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
