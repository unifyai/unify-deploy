#!/usr/bin/env python3
"""Ingest client data via the FileManager pipeline.

Reads ``pipeline_config.json``, translates it into a ``FilePipelineConfig``
via :meth:`PipelineConfig.to_fm_config`, and calls
``FileManager.ingest_files()``.  Supports any file format that
``FileParser`` handles (Excel, CSV, etc.).

Usage::

    uv run unity_deploy/customization/scripts/ingest_fm.py \\
        --client client_alpha --project ClientAlpha --deployment v1
    uv run unity_deploy/customization/scripts/ingest_fm.py \\
        --client client_alpha --project ClientAlpha \\
        --parallel --verbosity high --progress-file ./logs/fm_progress.jsonl
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest client data via the FileManager pipeline",
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
        "--parallel",
        action="store_true",
        help="Process files in parallel",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding (ingest rows only, no vectorization)",
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
    args = parser.parse_args()

    from unity_deploy.customization.scripts.ingest_utils import (
        initialize_environment,
        activate_project,
        load_pipeline_config,
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

    run_dir = create_run_directory("ingest_fm")
    log_file = args.log_file or str(run_dir / "run.log")

    project_root = initialize_environment(
        debug=args.debug,
        log_file=log_file,
        sdk_log=not args.no_sdk_log,
    )

    logger.info("=== FM Ingestion [%s / %s] ===", args.client, args.deployment)
    logger.info("Run directory: %s", run_dir)
    start_time = time.perf_counter()

    config = load_pipeline_config(
        args.config or default_pipeline_config_path(args.client, args.deployment),
        project_root=project_root,
    )

    file_paths = [sf.file_path for sf in config.source_files]
    logger.info("Source files: %s", file_paths)

    activate_project(args.project, overwrite=args.overwrite)

    cfg = config.to_fm_config()

    if args.no_embed:
        cfg.embed.strategy = "off"
    if args.parallel:
        cfg.execution.parallel_files = True
    cfg.diagnostics.enable_progress = True
    if args.verbosity:
        cfg.diagnostics.verbosity = args.verbosity
    if args.progress_file:
        cfg.diagnostics.progress_file = args.progress_file

    logger.info(
        "FM config: parallel=%s, embed_strategy=%s, progress=%s",
        cfg.execution.parallel_files,
        cfg.embed.strategy,
        cfg.diagnostics.enable_progress,
    )

    from unity.file_manager.managers.local import LocalFileManager

    fm = LocalFileManager(str(project_root))
    logger.info("Running FM ingest pipeline for %d file(s)...", len(file_paths))

    job = None
    job_store = None
    if args.job_tracking:
        from unity.common.pipeline import (
            DeploymentBundle,
            DeploymentBundleArtifact,
            DeploymentIdentity,
            LocalDeploymentBundleStore,
            LocalDeploymentJobStore,
            DeploymentIngestionJob,
            DeploymentObservabilityRefs,
        )
        from unity.common.pipeline._utils import utc_now_iso

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
                    logical_name=fp.split("/")[-1],
                    source_path=fp,
                )
                for fp in file_paths
            ],
        )
        bundle_ref = bundle_store.write_bundle(bundle)
        job = DeploymentIngestionJob(
            bundle_ref=bundle_ref,
            run_mode="file_manager",
            status="running",
            started_at=utc_now_iso(),
            observability_refs=DeploymentObservabilityRefs(
                log_file=log_file,
            ),
        )
        job_store.upsert_job(job)
        logger.info("Job tracking enabled: job_id=%s", job.job_id)

    result = fm.ingest_files(file_paths, config=cfg)

    elapsed = time.perf_counter() - start_time
    success = sum(
        1 for f in result.files.values() if getattr(f, "status", "") == "success"
    )
    errors = sum(
        1 for f in result.files.values() if getattr(f, "status", "") == "error"
    )

    if job is not None and job_store is not None:
        from unity.common.pipeline._utils import utc_now_iso

        job.status = "error" if errors > 0 else "success"
        job.finished_at = utc_now_iso()
        if errors > 0:
            job.error = f"{errors} file(s) failed"
        job.metadata["success_count"] = success
        job.metadata["error_count"] = errors
        job.metadata["total_records"] = result.total_records
        job_store.upsert_job(job)

    logger.info("=== FM Ingestion Complete ===")
    logger.info("  Client: %s, Deployment: %s", args.client, args.deployment)
    logger.info("  Total time: %.1fs", elapsed)
    logger.info("  Files: %d success, %d error", success, errors)
    logger.info("  Total rows ingested: %d", result.total_records)

    if errors > 0:
        for path, f in result.files.items():
            if getattr(f, "status", "") == "error":
                logger.error("  FAILED: %s -- %s", path, getattr(f, "error", "unknown"))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
