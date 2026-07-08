#!/usr/bin/env python3
"""Build a Builtins artifacts seed request from a provider manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11.
    try:
        tomllib = importlib.import_module("tomli")
    except ModuleNotFoundError as exc:  # pragma: no cover - configuration error.
        raise ModuleNotFoundError(
            "TOML manifest parsing requires Python 3.11+ or the 'tomli' package.",
        ) from exc

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
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open("rb") as file:
        return tomllib.load(file)


def _matching_sync_plans(
    manifest: dict[str, Any],
    *,
    backend_id: str | None,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
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
    return sync_plans


def _build_backend_request(
    provider_id: str,
    config: dict[str, Any],
    sync: dict[str, Any],
    *,
    environment: str,
    schema_version: int,
    workers: int,
    batch_size: int,
) -> dict[str, Any]:
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
        "schema_version": schema_version,
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


def build_request(
    manifest: dict[str, Any],
    *,
    environment: str,
    workers: int,
    batch_size: int,
    backend_id: str | None = None,
) -> dict[str, Any]:
    sync_plans = _matching_sync_plans(manifest, backend_id=backend_id)
    if len(sync_plans) != 1:
        raise ValueError(
            f"expected exactly one enabled provider sync, got {len(sync_plans)}",
        )
    provider_id, config, sync = sync_plans[0]
    return _build_backend_request(
        provider_id,
        config,
        sync,
        environment=environment,
        schema_version=manifest.get("schema_version", 1),
        workers=workers,
        batch_size=batch_size,
    )


def build_requests(
    manifest: dict[str, Any],
    *,
    environment: str,
    workers: int,
    batch_size: int,
    backend_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build one seed request per enabled provider sync in the manifest.

    Unlike ``build_request``, this does not require the manifest to resolve
    to a single provider: the Cloud Run seed job already seeds every enabled
    provider from the manifest in one run, so the deploy pre-flight step only
    needs a ``desired_hash``/``run_id`` per provider for status reporting.
    """
    sync_plans = _matching_sync_plans(manifest, backend_id=backend_id)
    if not sync_plans:
        raise ValueError(
            f"expected at least one enabled provider sync, got {len(sync_plans)}",
        )
    schema_version = manifest.get("schema_version", 1)
    return [
        _build_backend_request(
            provider_id,
            config,
            sync,
            environment=environment,
            schema_version=schema_version,
            workers=workers,
            batch_size=batch_size,
        )
        for provider_id, config, sync in sync_plans
    ]


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
    manifest = _load_manifest(Path(args.manifest))
    backend_id = args.backend_id or None
    if backend_id is not None:
        payload = build_request(
            manifest,
            environment=args.environment,
            workers=args.workers,
            batch_size=args.batch_size,
            backend_id=backend_id,
        )
    else:
        payloads = build_requests(
            manifest,
            environment=args.environment,
            workers=args.workers,
            batch_size=args.batch_size,
        )
        payload = payloads[0] if len(payloads) == 1 else {"requests": payloads}
    print(_json_dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
