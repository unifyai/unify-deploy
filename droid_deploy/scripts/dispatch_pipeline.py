#!/usr/bin/env python3
"""Operator dispatcher for the GCP parse/ingest pipeline.

Publishes one ``ParseRequested`` message per file to the parse topic,
uploading local files to GCS when needed.

Supports two input modes:

1. **Ad-hoc files** via ``--file`` / ``--files-from`` (one file per
   ``ParseRequested``).
2. **Config-driven** via ``--config pipeline_config.json`` which reads a
   :class:`PipelineConfig`, resolves paths, and derives per-file
   bindings automatically.  ``--limit N`` caps the number of source
   files dispatched (useful for smoke / HPA-scaling tests).

Both modes support ``--mode fm`` and ``--mode dm``.

All GCP settings come from environment variables via
:class:`droid_deploy.infra.gcp.settings.GcpPipelineSettings`
(``DROID_PUBSUB_PROJECT_ID``, ``DROID_GCS_ARTIFACT_BUCKET``,
``DROID_GCP_PIPELINE_ENVIRONMENT``).

Examples
--------

FM mode, single file::

    uv run droid_deploy/scripts/dispatch_pipeline.py \\
        --mode fm --file path/to/input.xlsx \\
        --user-id alice --assistant-id 42

DM mode, single file already in GCS::

    uv run droid_deploy/scripts/dispatch_pipeline.py \\
        --mode dm --file gs://bucket/foo.csv \\
        --user-id alice --assistant-id 42 \\
        --target-context "alice/42/Orders"

Config-driven FM dispatch (first 5 files)::

    uv run droid_deploy/scripts/dispatch_pipeline.py \\
        --mode fm --config pipeline_config.json \\
        --project-root ~/droid-deploy \\
        --user-id alice --assistant-id 42 --limit 5

Config-driven DM dispatch (bindings from config tables)::

    uv run droid_deploy/scripts/dispatch_pipeline.py \\
        --mode dm --config pipeline_config.json \\
        --project-root ~/droid-deploy \\
        --user-id alice --assistant-id 42

Config-driven DM dispatch into a shared team Data context::

    uv run unity_deploy/scripts/dispatch_pipeline.py \\
        --mode dm --config pipeline_config.json \\
        --project-root ~/unity-deploy \\
        --user-id alice --assistant-id 42 \\
        --destination team:54
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch files to the GCP parse/ingest pipeline.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["fm", "dm"],
        help=(
            "Ingestion mode: 'fm' routes via FileManager (FileRecords + "
            "per-file contexts); 'dm' routes via DataManager (flat target "
            "context)."
        ),
    )

    # -- file sources (ad-hoc or config-driven) ----------------------------

    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=[],
        metavar="PATH_OR_URI",
        help=(
            "Local path or gs:// URI to dispatch. Repeatable; publishes "
            "one ParseRequested per file."
        ),
    )
    parser.add_argument(
        "--files-from",
        default=None,
        metavar="MANIFEST",
        help=(
            "Path to a newline-separated file listing paths/URIs (one per "
            "line, blank lines and '#' comments ignored). May be combined "
            "with --file."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "Path to a pipeline_config.json. Source files and (in DM mode) "
            "per-file target contexts are derived from the config. May be "
            "combined with --file / --files-from."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=".",
        metavar="DIR",
        help=(
            "Root directory used to resolve relative file paths in "
            "--config (default: cwd)."
        ),
    )
    parser.add_argument(
        "--limit",
        default=None,
        type=int,
        metavar="N",
        help=(
            "Dispatch only the first N source files from --config "
            "(useful for smoke / HPA-scaling tests). Ignored for "
            "--file / --files-from."
        ),
    )

    # -- identity ----------------------------------------------------------

    parser.add_argument(
        "--user-id",
        default=None,
        help=(
            "IngestBinding.user_id (falls back to $USER_ID). Required "
            "for both --mode fm and --mode dm."
        ),
    )
    parser.add_argument(
        "--assistant-id",
        default=None,
        help=(
            "IngestBinding.assistant_id (falls back to $ASSISTANT_ID). "
            "Required for both --mode fm and --mode dm."
        ),
    )

    fm_group = parser.add_argument_group("FM mode (--mode fm)")
    fm_group.add_argument(
        "--alias",
        default="Local",
        help="FmBinding.fm_alias (default: 'Local').",
    )

    dm_group = parser.add_argument_group("DM mode (--mode dm)")
    dm_group.add_argument(
        "--target-context",
        default=None,
        help=(
            "DmBinding.target_context (required for --mode dm with "
            "--file/--files-from; derived from config tables when "
            "using --config)."
        ),
    )
    dm_group.add_argument(
        "--create-table-prefix",
        default="",
        help="DmBinding.create_table_prefix (default: '').",
    )
    dm_group.add_argument(
        "--destination",
        default="personal",
        metavar="personal|team:<id>",
        help=(
            "Which root/scope the resolved --target-context is written "
            "under (a separate axis from the context path itself). "
            "Accepted values: 'personal' (default) -> the dispatching "
            "assistant's own Data root {user}/{assistant}/Data/<ctx>; "
            "'team:<id>' -> the shared team Data root Teams/<id>/Data/<ctx> "
            "that every member assistant can read (the ingest worker "
            "validates team membership before honouring it). Only "
            "meaningful for --mode dm."
        ),
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # -- normalize the write destination -----------------------------------
    # ``canonical_destination`` is the single source of truth for the
    # "personal" / "team:<id>" grammar: it returns ``None`` for personal
    # and ``"team:<id>"`` for shared, raising on anything else.
    from droid.common.context_registry import ContextRegistry

    try:
        args.destination = ContextRegistry.canonical_destination(args.destination)
    except ValueError as exc:
        logger.error("--destination is invalid: %s", exc)
        return 2
    if args.destination is not None and args.mode != "dm":
        logger.error(
            "--destination %s is only supported with --mode dm.",
            args.destination,
        )
        return 2

    # -- build dispatch items from ad-hoc files ----------------------------
    dispatch_items = _collect_adhoc_items(args)

    # -- build dispatch items from config ----------------------------------
    if args.config:
        dispatch_items.extend(_collect_config_items(args))

    if not dispatch_items:
        logger.error(
            "No files to dispatch. Supply --file, --files-from, or --config.",
        )
        return 2

    # For ad-hoc DM files without a config, --target-context is required.
    adhoc_files = _collect_files(
        explicit=list(args.files),
        manifest=args.files_from,
    )
    if args.mode == "dm" and adhoc_files and not args.target_context:
        logger.error(
            "--mode dm with --file/--files-from requires --target-context.",
        )
        return 2

    return _dispatch(args=args, items=dispatch_items)


# ---------------------------------------------------------------------------
# Dispatch item: (path_or_uri, per_file_context_override_or_None)
# ---------------------------------------------------------------------------

DispatchItem = tuple[str, Optional[str], Optional[dict]]
"""(path_or_uri, dm_target_context_override, table_config).

For FM mode or ad-hoc DM dispatch the second element is ``None``
(bindings are derived from CLI args). For config-driven DM dispatch
it carries the ``tables[0].context`` from the config.

``table_config`` (third element) is a dict keyed by sheet/table name
carrying per-table metadata (description, embed_columns, chunk_size,
column_descriptions, post_ingest) resolved from ``PipelineConfig``.
``None`` for ad-hoc dispatches.
"""


def _collect_adhoc_items(args: argparse.Namespace) -> list[DispatchItem]:
    """Build dispatch items from --file / --files-from."""
    files = _collect_files(
        explicit=list(args.files),
        manifest=args.files_from,
    )
    return [(f, None, None) for f in files]


def _collect_config_items(args: argparse.Namespace) -> list[DispatchItem]:
    """Build dispatch items from --config."""
    from droid_deploy.assistant_deployments.types.pipeline_config import (
        PipelineConfig,
        build_table_config_for_source_file,
    )

    config = PipelineConfig.from_file(args.config)
    config.resolve_paths(Path(args.project_root).resolve())

    source_files = config.source_files
    if args.limit is not None and args.limit > 0:
        source_files = source_files[: args.limit]

    total_tables = sum(len(sf.tables) for sf in source_files)
    logger.info(
        "Config %s: %d source file(s), %d table(s)%s",
        args.config,
        len(source_files),
        total_tables,
        f" (limited to first {args.limit})" if args.limit else "",
    )

    items: list[DispatchItem] = []
    for sf in source_files:
        dm_context: Optional[str] = None
        if args.mode == "dm":
            tables = list(sf.tables or [])
            if not tables:
                logger.warning(
                    "Skipping %s: no tables defined in config",
                    sf.file_path,
                )
                continue
            dm_context = tables[0].context

        table_cfg = (
            build_table_config_for_source_file(config, sf) if sf.tables else None
        )
        items.append((sf.file_path, dm_context, table_cfg))

    return items


def _collect_files(*, explicit: list[str], manifest: Optional[str]) -> list[str]:
    """Merge --file args with a manifest file, preserving order + dedup."""
    seen: set[str] = set()
    out: list[str] = []
    for f in explicit:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    if manifest:
        for raw in Path(manifest).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                out.append(line)
    return out


def _dispatch(
    *,
    args: argparse.Namespace,
    items: list[DispatchItem],
) -> int:
    import os

    from google.cloud import storage

    from droid.common.pipeline import DispatchTarget, publish_parse_request
    from droid.common.pipeline.deployment.types import (
        DeploymentBundleRef,
        DeploymentIngestionJob,
        DispatchManifest,
    )
    from droid.common.pipeline.types import DmBinding, FmBinding
    from droid_deploy.infra.gcp.artifact_store import GcsArtifactStore
    from droid_deploy.infra.gcp.deployment_stores import GcsDeploymentJobStore
    from droid_deploy.infra.gcp.settings import GcpPipelineSettings

    settings = GcpPipelineSettings()
    project_id = settings.pubsub.project_id
    bucket_name = settings.artifact_store.bucket
    if not project_id:
        logger.error("DROID_PUBSUB_PROJECT_ID is not set; cannot dispatch.")
        return 2
    if not bucket_name:
        logger.error("DROID_GCS_ARTIFACT_BUCKET is not set; cannot dispatch.")
        return 2

    target = DispatchTarget(
        project_id=project_id,
        bucket_name=bucket_name,
        env_suffix=settings.env_suffix(),
    )

    user_id = args.user_id or os.environ.get("USER_ID")
    if not user_id:
        logger.error(
            "--user-id is required (or USER_ID in the env). The ingest "
            "worker needs it for provenance / routing.",
        )
        return 2
    assistant_id = args.assistant_id or os.environ.get("ASSISTANT_ID")
    if not assistant_id:
        logger.error(
            "--assistant-id is required (or ASSISTANT_ID in the env). "
            "Current worker dispatch is assistant-scoped, so the ingest "
            "worker resolves the Unify api key per message via "
            "GET /v0/admin/assistant?agent_id=....",
        )
        return 2

    storage_client = storage.Client(project=project_id)
    artifact_store = GcsArtifactStore(
        client=storage_client,
        settings=settings.artifact_store,
    )
    job_store = GcsDeploymentJobStore(artifact_store=artifact_store)

    dispatch_id = uuid4().hex

    logger.info(
        "=== Dispatch %s [mode=%s, env=%s, user_id=%s, assistant_id=%s, "
        "destination=%s] ===",
        dispatch_id,
        args.mode,
        settings.environment,
        user_id,
        assistant_id,
        args.destination or "personal",
    )
    logger.info("Dispatching %d file(s)...", len(items))

    errors = 0
    job_ids: list[str] = []
    for path_or_uri, dm_context_override, table_config in items:
        fm_binding: Optional[FmBinding] = None
        dm_binding: Optional[DmBinding] = None
        if args.mode == "fm":
            fm_binding = FmBinding(
                user_id=user_id,
                assistant_id=assistant_id,
                fm_alias=args.alias,
                logical_path=path_or_uri,
            )
        else:
            dm_binding = DmBinding(
                user_id=user_id,
                assistant_id=assistant_id,
                target_context=(dm_context_override or args.target_context or ""),
                create_table_prefix=args.create_table_prefix,
                destination=args.destination,
            )

        source_kwargs: dict = {}
        if path_or_uri.startswith("gs://"):
            source_kwargs["source_gs_uri"] = path_or_uri
        else:
            source_kwargs["source_local_path"] = path_or_uri

        try:
            result = publish_parse_request(
                target=target,
                logical_path=path_or_uri,
                ingestion_mode=args.mode,
                fm_binding=fm_binding,
                dm_binding=dm_binding,
                dispatch_id=dispatch_id,
                table_config=table_config,
                **source_kwargs,
            )

            job = DeploymentIngestionJob(
                job_id=result.job_id,
                dispatch_id=dispatch_id,
                bundle_ref=DeploymentBundleRef(
                    bundle_id=result.job_id,
                    manifest_path="",
                ),
                run_mode="file_manager" if args.mode == "fm" else "data_manager",
                execution_target="staging",
                status="queued",
            )
            job_store.upsert_job(job)
            job_ids.append(result.job_id)

            logger.info(
                "  dispatched %s -> job=%s gs_uri=%s message_id=%s",
                path_or_uri,
                result.job_id,
                result.gs_uri,
                result.message_id,
            )
        except Exception:
            logger.exception("  dispatch failed for %s", path_or_uri)
            errors += 1

    if job_ids:
        manifest = DispatchManifest(
            dispatch_id=dispatch_id,
            source="dispatch_pipeline",
            mode=args.mode,
            config_path=args.config or "",
            job_ids=job_ids,
            total_files=len(items),
        )
        job_store.write_dispatch(manifest)

    print(
        f"\n=== Dispatch {dispatch_id} complete "
        f"({len(job_ids)} dispatched, {errors} errors) ===",
    )
    print("  List:    pipeline_control list")
    print(f"  Status:  pipeline_control status --dispatch-id {dispatch_id}")
    print(f"  Cancel:  pipeline_control cancel --dispatch-id {dispatch_id}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
