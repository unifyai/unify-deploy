#!/usr/bin/env python3
"""Ingest client data via the DataManager pipeline.

Parses source files (Excel, CSV, or any format supported by
``FileParser``) using ``FileParser.parse_batch()``, then ingests each
``ExtractedTable`` into named contexts via ``DataManager.ingest()``.

Uses the shared ``PipelineInstrumentation`` for run/cost ledgers, stage/file
manifests, and cost tracking -- identical to the FM pipeline path.

Usage::

    uv run droid_deploy/assistant_deployments/scripts/ingest_dm.py \\
        --client client_alpha --project ClientAlpha --deployment v1
    uv run droid_deploy/assistant_deployments/scripts/ingest_dm.py \\
        --client client_alpha --project ClientAlpha \\
        --no-embed --verbosity high --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from droid_deploy.assistant_deployments.types.pipeline_config import (
        PipelineConfig,
        SourceTableSpec,
    )

logger = logging.getLogger(__name__)


def _resolve_embed_columns(
    config: PipelineConfig,
    file_path: str,
    sheet_name: str,
) -> Optional[List[str]]:
    """Look up embed source columns for a given file + sheet from config."""
    for spec in config.embed.file_specs:
        if spec.file_path in file_path or spec.file_path == "*":
            for table_spec in spec.tables:
                if table_spec.table == sheet_name:
                    return list(table_spec.source_columns)
    return None


def _dispatch_dm(
    *,
    config: PipelineConfig,
    project_name: str,
    user_id: str,
    assistant_id: str,
) -> int:
    """Publish one ParseRequested per source_file with DM-mode binding.

    Uploads each source file to GCS via
    :func:`droid.common.pipeline.publish_parse_request`, which also
    enforces the ``one file per ParseRequested`` invariant that Tier-2
    parallelism depends on.

    Each file's ``DmBinding.target_context`` is taken from the first
    table's ``context``. If a source file has multiple tables with
    differing contexts, a warning is logged and the first context is
    used; the worker's current DM mode applies that single binding to
    every table in the plan. Extending :class:`DmBinding` to carry a
    per-table mapping is out of scope for this flag -- the common case
    is one-table-per-file in DM pipelines.

    Current DM dispatch is assistant-scoped too: ``user_id`` is carried
    for provenance / routing and ``assistant_id`` identifies the
    assistant whose Orchestra-bound api key authorizes the ingest via
    ``GET /v0/admin/assistant?agent_id=...``.
    """
    from droid.common.pipeline import DispatchTarget, publish_parse_request
    from droid.common.pipeline.types import DmBinding
    from droid_deploy.infra.gcp.settings import GcpPipelineSettings

    settings = GcpPipelineSettings()
    project_id = settings.pubsub.project_id
    bucket_name = settings.artifact_store.bucket
    if not project_id:
        logger.error(
            "DROID_PUBSUB_PROJECT_ID is not set; cannot dispatch. Set the "
            "GCP project via env or unset --dispatch to run in-process.",
        )
        return 2
    if not bucket_name:
        logger.error(
            "DROID_GCS_ARTIFACT_BUCKET is not set; cannot dispatch.",
        )
        return 2

    target = DispatchTarget(
        project_id=project_id,
        bucket_name=bucket_name,
        env_suffix=settings.env_suffix(),
    )

    logger.info(
        "=== DM Dispatch [project=%s, env=%s, bucket=%s, user_id=%s, assistant_id=%s] ===",
        project_name,
        settings.environment,
        bucket_name,
        user_id,
        assistant_id,
    )
    logger.info("Dispatching %d source file(s)...", len(config.source_files))

    errors = 0
    for sf in config.source_files:
        tables = list(sf.tables or [])
        if not tables:
            logger.warning("Skipping %s: no tables defined in config", sf.file_path)
            continue

        contexts = {t.context for t in tables}
        if len(contexts) > 1:
            logger.warning(
                "File %s has %d distinct table contexts (%s); using the first "
                "(%s) for DmBinding. All tables will land under this context.",
                sf.file_path,
                len(contexts),
                sorted(contexts),
                tables[0].context,
            )

        dm_binding = DmBinding(
            user_id=user_id,
            assistant_id=assistant_id,
            target_context=tables[0].context,
        )
        try:
            result = publish_parse_request(
                target=target,
                logical_path=sf.file_path,
                ingestion_mode="dm",
                dm_binding=dm_binding,
                source_local_path=sf.file_path,
            )
            logger.info(
                "  dispatched %s -> job=%s gs_uri=%s message_id=%s",
                sf.file_path,
                result.job_id,
                result.gs_uri,
                result.message_id,
            )
        except Exception:
            logger.exception("  dispatch failed for %s", sf.file_path)
            errors += 1

    logger.info(
        "=== DM Dispatch Complete (files=%d, errors=%d) ===",
        len(config.source_files),
        errors,
    )
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest client data via the DataManager pipeline",
    )
    parser.add_argument(
        "--client",
        required=True,
        metavar="NAME",
        help="Client package name under clients/, e.g. client_alpha",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to pipeline_config.json (when omitted: "
            "clients/CLIENT/deployments/DEPLOYMENT/data/pipeline_config.json)"
        ),
    )
    parser.add_argument(
        "--deployment",
        default="v1",
        metavar="NAME",
        help="Deployment folder under deployments/, e.g. v0 or v1 (default: v1)",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Unify project name",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete and recreate the project",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding (ingest rows only, no vectorization)",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Ingest tables in parallel",
    )
    parser.add_argument(
        "--verbosity",
        choices=["low", "medium", "high"],
        default=None,
        help="Progress verbosity level",
    )
    parser.add_argument(
        "--progress-file",
        default=None,
        help="Progress JSONL output path (overrides run dir)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file path (default: <run_dir>/run.log)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    parser.add_argument(
        "--no-sdk-log",
        action="store_true",
        help="Disable the companion *_unify.log SDK log file",
    )
    parser.add_argument(
        "--skip-all-context",
        action="store_true",
        help="Skip add_to_all_context (faster bulk load; add references later)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="Override per-table chunk_size from config (e.g. 250 for faster API calls)",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        default=None,
        metavar="LABEL",
        help=(
            "Only ingest the listed table(s) by sheet label (case-sensitive "
            "substring match)."
        ),
    )
    parser.add_argument(
        "--merge-configs",
        default=None,
        metavar="DIR",
        help=(
            "Merge individual per-table pipeline configs from DIR into a "
            "single consolidated config, write it to --config (or the default "
            "pipeline_config.json for the client/deployment), and exit "
            "without running ingestion."
        ),
    )
    parser.add_argument(
        "--job-tracking",
        action="store_true",
        help="Enable deployment job tracking (writes job status to .deployments/)",
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help=(
            "Publish ParseRequested messages to the GCP pipeline (one per "
            "source file) and exit, instead of parsing/ingesting in-process. "
            "Each file is sent with ingestion_mode=dm + DmBinding. Use this "
            "to drive the GKE parse/ingest workers for bulk operator "
            "ingests in staging/production."
        ),
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help=(
            "DmBinding.user_id used for --dispatch. The ingest worker "
            "keeps this for provenance / routing. Falls back to the "
            "USER_ID environment variable when omitted."
        ),
    )
    parser.add_argument(
        "--assistant-id",
        default=None,
        help=(
            "DmBinding.assistant_id used for --dispatch. The ingest worker "
            "resolves the Unify api key per message via Orchestra's "
            "GET /v0/admin/assistant?agent_id=... . Falls back to the "
            "ASSISTANT_ID environment variable when omitted."
        ),
    )
    args = parser.parse_args()

    from droid_deploy.assistant_deployments.scripts.ingest_utils import (
        initialize_environment,
        activate_project,
        load_pipeline_config,
        create_pipeline_reporter,
        create_run_directory,
        merge_pipeline_configs,
        default_pipeline_config_path,
    )

    if args.merge_configs:
        project_root = initialize_environment(debug=args.debug, sdk_log=False)
        output = args.config or default_pipeline_config_path(
            args.client,
            args.deployment,
        )
        merged = merge_pipeline_configs(args.merge_configs, output)
        logger.info(
            "Merged %d source files -> %s",
            len(merged.source_files),
            output,
        )
        return 0

    run_dir = create_run_directory("ingest_dm")
    log_file = args.log_file or str(run_dir / "run.log")

    project_root = initialize_environment(
        debug=args.debug,
        log_file=log_file,
        sdk_log=not args.no_sdk_log,
    )

    logger.info("=== DM Ingestion [%s / %s] ===", args.client, args.deployment)
    logger.info("Run directory: %s", run_dir)
    pipeline_start = time.perf_counter()

    config = load_pipeline_config(
        args.config or default_pipeline_config_path(args.client, args.deployment),
        project_root=project_root,
    )

    if args.dispatch:
        import os

        user_id = args.user_id or os.environ.get("USER_ID")
        assistant_id = args.assistant_id or os.environ.get("ASSISTANT_ID")
        if not user_id:
            logger.error(
                "--dispatch requires --user-id (or USER_ID in the env). "
                "A real user identity is required for provenance / routing.",
            )
            return 2
        if not assistant_id:
            logger.error(
                "--dispatch requires --assistant-id (or ASSISTANT_ID in the env). "
                "Current DM dispatch is assistant-scoped, so the ingest worker "
                "resolves the Unify api key per message via "
                "GET /v0/admin/assistant?agent_id=....",
            )
            return 2
        return _dispatch_dm(
            config=config,
            project_name=args.project,
            user_id=user_id,
            assistant_id=assistant_id,
        )

    activate_project(args.project, overwrite=args.overwrite)

    diagnostics = config.diagnostics.model_dump()
    reporter, run_dir = create_pipeline_reporter(
        diagnostics,
        "ingest_dm",
        run_dir=run_dir,
        progress_file_override=args.progress_file,
        verbosity_override=args.verbosity,
    )
    verbosity = args.verbosity or config.diagnostics.verbosity

    from droid.file_manager.managers.utils.progress import create_progress_event
    from droid.common.pipeline import (
        ArtifactWorkItem,
        InlineRowsHandle,
        PipelineInstrumentation,
        build_table_handles,
        ingest_artifacts,
    )

    # ------------------------------------------------------------------ #
    # Step 1: Parse source files
    # ------------------------------------------------------------------ #

    from droid.file_manager.file_parsers import FileParser
    from droid.file_manager.file_parsers.types.contracts import (
        FileParseRequest,
        FileParseResult,
    )

    file_parser = FileParser()

    parse_requests: list[FileParseRequest] = []
    for sf in config.source_files:
        parse_requests.append(
            FileParseRequest(
                logical_path=sf.file_path,
                source_local_path=sf.file_path,
            ),
        )
        logger.info("Queued for parsing: %s", sf.file_path)

    parse_start = time.perf_counter()
    for req in parse_requests:
        reporter.report(
            create_progress_event(
                req.logical_path,
                "parse",
                "started",
                duration_ms=0.0,
                elapsed_ms=0.0,
                verbosity=verbosity,
            ),
        )

    logger.info("Parsing %d file(s)...", len(parse_requests))
    parse_results: list[FileParseResult] = file_parser.parse_batch(
        parse_requests,
        raises_on_error=False,
        parse_config=config.parse,
    )

    parse_duration_ms = (time.perf_counter() - parse_start) * 1000

    for pr in parse_results:
        lp = str(getattr(pr, "logical_path", "") or "")
        status = str(getattr(pr, "status", "error"))
        elapsed_ms = (time.perf_counter() - pipeline_start) * 1000

        if status == "success":
            table_count = len(pr.tables) if pr.tables else 0
            reporter.report(
                create_progress_event(
                    lp,
                    "parse",
                    "completed",
                    duration_ms=parse_duration_ms,
                    elapsed_ms=elapsed_ms,
                    meta={"table_count": table_count},
                    verbosity=verbosity,
                ),
            )
            logger.info(
                "Parsed %s: %d table(s) extracted",
                lp,
                table_count,
            )
        else:
            error_msg = str(getattr(pr, "error", "") or "unknown parse error")
            reporter.report(
                create_progress_event(
                    lp,
                    "parse",
                    "failed",
                    duration_ms=parse_duration_ms,
                    elapsed_ms=elapsed_ms,
                    error=error_msg,
                    verbosity=verbosity,
                ),
            )
            logger.error("Parse failed for %s: %s", lp, error_msg)

    # ------------------------------------------------------------------ #
    # Step 2: Ingest each ExtractedTable via DataManager.ingest()
    # ------------------------------------------------------------------ #

    from droid.data_manager.data_manager import DataManager

    dm = DataManager()

    table_specs: dict[str, SourceTableSpec] = {}
    for sf in config.source_files:
        for ts in sf.tables:
            table_specs[ts.sheet] = ts

    sheet_col_descs: dict[str, dict[str, str]] = {}
    if config.ingest.business_contexts:
        for fc in config.ingest.business_contexts.file_contexts:
            for tc in fc.table_contexts:
                if tc.column_descriptions:
                    sheet_col_descs[tc.table] = tc.column_descriptions

    embed_strategy = "off" if args.no_embed else config.embed.strategy
    infer_untyped = config.ingest.infer_untyped_fields
    max_table_workers = config.execution.max_table_workers

    # Set up instrumentation for run/cost ledgers and manifests
    parallel = args.parallel and True
    total_file_count = sum(
        1 for pr in parse_results if pr.status == "success" and pr.tables
    )
    instrumentation = PipelineInstrumentation.from_config(
        config,
        parallel_files=parallel,
        file_count=total_file_count,
        meta={
            "pipeline": "ingest_dm",
            "client": args.client,
            "deployment": args.deployment,
            "embed_strategy": embed_strategy,
        },
    )

    # Record parse costs
    if instrumentation.has_cost_tracking:
        from droid.file_manager.managers.utils.executor import (
            _extract_parse_cost_metrics,
        )

        for pr in parse_results:
            lp = str(getattr(pr, "logical_path", "") or "")
            metrics = _extract_parse_cost_metrics(pr, lp, config.parse)
            instrumentation.add_parse_costs(file_path=lp, **metrics)

    # Optional deployment job tracking
    job = None
    job_store = None
    if args.job_tracking:
        from droid.common.pipeline import (
            DeploymentBundle,
            DeploymentBundleArtifact,
            DeploymentIdentity,
            LocalDeploymentBundleStore,
            LocalDeploymentJobStore,
            DeploymentIngestionJob,
            DeploymentObservabilityRefs,
        )
        from droid.common.pipeline._utils import utc_now_iso

        deploy_root = run_dir / ".deployments"
        bundle_store = LocalDeploymentBundleStore(deploy_root)
        job_store = LocalDeploymentJobStore(deploy_root)

        bundle = DeploymentBundle(
            deployment_identity=DeploymentIdentity(
                client=args.client,
                deployment=args.deployment,
                project=args.project,
            ),
            source_artifact_manifest=[
                DeploymentBundleArtifact(
                    kind="source_data",
                    logical_name=sf.file_path.split("/")[-1],
                    source_path=sf.file_path,
                )
                for sf in config.source_files
            ],
        )
        bundle_ref = bundle_store.write_bundle(bundle)
        job = DeploymentIngestionJob(
            bundle_ref=bundle_ref,
            run_mode="data_manager",
            status="running",
            started_at=utc_now_iso(),
            observability_refs=DeploymentObservabilityRefs(
                log_file=log_file,
            ),
        )
        job_store.upsert_job(job)
        logger.info("Job tracking enabled: job_id=%s", job.job_id)

    total_tables = 0
    total_rows = 0
    failed_tables = 0
    ingested_contexts: list[str] = []

    def _make_chunk_callback(lp: str, sheet_name: str):
        def _on_chunk(task, result):
            meta = dict(task.metadata)
            meta["table_label"] = sheet_name
            meta["task_type"] = task.task_type
            meta["task_id"] = task.id
            meta["success"] = result.success
            meta["duration_ms"] = result.duration_ms
            meta["retries"] = result.retries
            if result.value and isinstance(result.value, dict):
                val = dict(result.value)
                if "inserted_ids" in val:
                    val["inserted_count"] = len(val.pop("inserted_ids"))
                meta.update(val)

            phase = f"ingest_table/{task.task_type}"
            chunk_status = "completed" if result.success else "failed"
            elapsed_ms = (time.perf_counter() - pipeline_start) * 1000

            reporter.report(
                create_progress_event(
                    lp,
                    phase,
                    chunk_status,
                    duration_ms=result.duration_ms,
                    elapsed_ms=elapsed_ms,
                    error=result.error if not result.success else None,
                    meta=meta,
                    verbosity=verbosity,
                ),
            )
            logger.info(
                "  [%s] %s %s (%.0fms) batch=%s/%s",
                sheet_name,
                task.task_type,
                chunk_status,
                result.duration_ms,
                meta.get("chunk_index", "?"),
                meta.get("total_chunks", "?"),
            )

        return _on_chunk

    with instrumentation:
        for pr in parse_results:
            if pr.status != "success" or not pr.tables:
                if pr.status != "success":
                    instrumentation.record_file(
                        file_path=str(getattr(pr, "logical_path", "") or ""),
                        status="error",
                        meta={"parse_error": str(getattr(pr, "error", "") or "")},
                    )
                continue

            lp = str(pr.logical_path)
            file_start = time.perf_counter()

            # Build transport handles so rows are streamed from source
            # files (CSV/XLSX) instead of materialised in ExtractedTable.rows.
            table_handles = build_table_handles(pr)

            work_items: list[ArtifactWorkItem] = []
            for t in pr.tables:
                label = t.sheet_name or t.label
                spec = table_specs.get(label)
                if spec is None:
                    continue
                if args.tables and not any(f in label for f in args.tables):
                    logger.info("Skipping '%s' (not in --tables filter)", label)
                    continue

                tid = str(getattr(t, "table_id", "") or "")
                handle = table_handles.get(tid)

                if handle is None:
                    inline = list(getattr(t, "rows", []) or [])
                    if not inline:
                        logger.info(
                            "Sheet '%s' has 0 rows -- skipping %s",
                            label,
                            spec.context,
                        )
                        continue
                    handle = InlineRowsHandle(
                        rows=inline,
                        columns=list(getattr(t, "columns", []) or []),
                        row_count=len(inline),
                    )

                handle_row_count = getattr(handle, "row_count", None) or 0
                if isinstance(handle, InlineRowsHandle) and not handle.rows:
                    logger.info(
                        "Sheet '%s' has 0 rows -- skipping %s",
                        label,
                        spec.context,
                    )
                    continue

                chunk_size = args.chunk_size or spec.chunk_size
                embed_columns = (
                    _resolve_embed_columns(config, lp, label)
                    if not args.no_embed
                    else None
                )
                col_descs = sheet_col_descs.get(label, {})
                post_ingest_config = config.effective_post_ingest(spec)

                rows_payload = None
                handle_payload = None
                if isinstance(handle, InlineRowsHandle):
                    rows_payload = list(handle.rows)
                else:
                    handle_payload = handle

                work_items.append(
                    ArtifactWorkItem(
                        kind="table",
                        label=label,
                        stage_name="ingest_table",
                        payload={
                            "context_path": spec.context,
                            "rows": rows_payload,
                            "table_input_handle": handle_payload,
                            "description": spec.description or None,
                            "embed_columns": embed_columns,
                            "embed_strategy": (
                                embed_strategy if embed_columns else "off"
                            ),
                            "chunk_size": chunk_size,
                            "infer_untyped": infer_untyped,
                            "skip_all_context": args.skip_all_context,
                            "post_ingest_config": post_ingest_config,
                            "col_descs": col_descs,
                            "chunk_callback": _make_chunk_callback(lp, label),
                        },
                        columns=list(getattr(t, "columns", []) or []),
                        row_count=handle_row_count,
                        table_id=tid or None,
                        stage_id=instrumentation.make_stage_id(
                            file_path=lp,
                            stage_name="ingest_table",
                            discriminator=label,
                        ),
                        meta={
                            "table_label": label,
                            "row_count": handle_row_count,
                            "context": spec.context,
                            "chunk_size": chunk_size,
                        },
                    ),
                )

            # DM-specific ingest function
            def _dm_ingest_fn(item: ArtifactWorkItem) -> dict:
                p = item.payload
                fields = (
                    {
                        name: {"description": desc}
                        for name, desc in p["col_descs"].items()
                    }
                    if p["col_descs"]
                    else None
                )
                result = dm.ingest(
                    p["context_path"],
                    p["rows"],
                    table_input_handle=p.get("table_input_handle"),
                    description=p["description"],
                    fields=fields,
                    embed_columns=p["embed_columns"],
                    embed_strategy=p["embed_strategy"],
                    chunk_size=p["chunk_size"],
                    infer_untyped_fields=p["infer_untyped"],
                    add_to_all_context=not p["skip_all_context"],
                    post_ingest=p["post_ingest_config"],
                    on_task_complete=p["chunk_callback"],
                    expected_total_rows=item.row_count,
                )
                return {
                    "ingest_result": result,
                    "context": p["context_path"],
                    "rows_inserted": result.rows_inserted,
                    "rows_embedded": result.rows_embedded,
                }

            if work_items:
                artifact_results = ingest_artifacts(
                    work_items=work_items,
                    ingest_fn=_dm_ingest_fn,
                    instrumentation=instrumentation,
                    source_path=lp,
                    max_workers=max_table_workers if parallel else 1,
                    retry_config=config.retry if hasattr(config, "retry") else None,
                )

                file_failures = 0
                for ar in artifact_results:
                    if ar.success:
                        rows_inserted = 0
                        if ar.value and isinstance(ar.value, dict):
                            rows_inserted = ar.value.get("rows_inserted", 0)
                            ctx = ar.value.get("context", "")
                            if ctx:
                                ingested_contexts.append(ctx)
                        total_tables += 1
                        total_rows += rows_inserted
                    else:
                        file_failures += 1
                        failed_tables += 1

                    elapsed_ms = (time.perf_counter() - pipeline_start) * 1000
                    reporter.report(
                        create_progress_event(
                            lp,
                            "ingest_table",
                            "completed" if ar.success else "failed",
                            duration_ms=ar.duration_ms,
                            elapsed_ms=elapsed_ms,
                            error=ar.error,
                            meta={"table_label": ar.label},
                            verbosity=verbosity,
                        ),
                    )

                file_duration_ms = (time.perf_counter() - file_start) * 1000
                file_status = "success" if file_failures == 0 else "error"

                instrumentation.record_file(
                    file_path=lp,
                    status=file_status,
                    total_duration_ms=file_duration_ms,
                    meta={
                        "tables_ingested": len(artifact_results) - file_failures,
                        "ingest_failures": file_failures,
                    },
                )

                elapsed_ms = (time.perf_counter() - pipeline_start) * 1000
                reporter.report(
                    create_progress_event(
                        lp,
                        "file_complete",
                        "completed" if file_failures == 0 else "failed",
                        duration_ms=file_duration_ms,
                        elapsed_ms=elapsed_ms,
                        meta={
                            "tables_ingested": len(artifact_results) - file_failures,
                            "rows_ingested": total_rows,
                            "ingest_failures": file_failures,
                        },
                        verbosity=verbosity,
                    ),
                )

        instrumentation.add_observability_costs()

    # Finalize deployment job tracking
    if job is not None and job_store is not None:
        from droid.common.pipeline._utils import utc_now_iso

        job.status = "error" if failed_tables > 0 else "success"
        job.finished_at = utc_now_iso()
        if failed_tables > 0:
            job.error = f"{failed_tables} table(s) failed"
        job.metadata["total_tables"] = total_tables
        job.metadata["total_rows"] = total_rows
        job.metadata["failed_tables"] = failed_tables
        job_store.upsert_job(job)

    reporter.flush()

    elapsed = time.perf_counter() - pipeline_start

    logger.info("=== DM Ingestion Complete ===")
    logger.info("  Client: %s, Deployment: %s", args.client, args.deployment)
    logger.info("  Run directory: %s", run_dir)
    logger.info("  Total time: %.1fs", elapsed)
    logger.info("  Tables ingested: %d", total_tables)
    logger.info("  Total rows: %d", total_rows)
    logger.info("  Failed tables: %d", failed_tables)
    logger.info("  Embed strategy: %s", embed_strategy)
    logger.info("  Contexts created:")
    for ctx in ingested_contexts:
        logger.info("    - %s", ctx)

    return 1 if failed_tables > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
