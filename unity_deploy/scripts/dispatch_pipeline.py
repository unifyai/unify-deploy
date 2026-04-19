#!/usr/bin/env python3
"""Ad-hoc operator dispatcher for the GCP parse/ingest pipeline.

Publishes one ``ParseRequested`` message per file to the parse topic,
uploading local files to GCS when needed. This is the lightest-weight
entrypoint for manually kicking off ingestion against staging or
production workers -- for full pipeline configs, prefer
``ingest_dm.py --dispatch`` or ``ingest_fm.py --dispatch`` which read
``pipeline_config.json`` and derive bindings automatically.

All GCP settings come from environment variables via
:class:`unity_deploy.infra.gcp.settings.GcpPipelineSettings`
(``UNITY_PUBSUB_PROJECT_ID``, ``UNITY_GCS_ARTIFACT_BUCKET``,
``UNITY_GCP_PIPELINE_ENVIRONMENT``).

Examples
--------

FM mode, single file::

    uv run unity_deploy/scripts/dispatch_pipeline.py \\
        --mode fm --file path/to/input.xlsx \\
        --user-id alice --assistant-id 42

DM mode, single file already in GCS::

    uv run unity_deploy/scripts/dispatch_pipeline.py \\
        --mode dm --file gs://bucket/foo.csv \\
        --target-context "alice/42/Orders"

Bulk dispatch from a newline-separated manifest (one file per line)::

    uv run unity_deploy/scripts/dispatch_pipeline.py \\
        --mode fm --files-from manifest.txt \\
        --user-id alice --assistant-id 42
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

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
    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=[],
        metavar="PATH_OR_URI",
        help=(
            "Local path or gs:// URI to dispatch. Repeatable; publishes "
            "one ParseRequested per invocation."
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

    fm_group = parser.add_argument_group("FM mode (--mode fm)")
    fm_group.add_argument(
        "--user-id",
        default=None,
        help="FmBinding.user_id (falls back to $USER_ID, then 'default').",
    )
    fm_group.add_argument(
        "--assistant-id",
        default=None,
        help="FmBinding.assistant_id (falls back to $ASSISTANT_ID, then '0').",
    )
    fm_group.add_argument(
        "--alias",
        default="Local",
        help="FmBinding.fm_alias (default: 'Local').",
    )

    dm_group = parser.add_argument_group("DM mode (--mode dm)")
    dm_group.add_argument(
        "--target-context",
        default=None,
        help="DmBinding.target_context (required when --mode dm).",
    )
    dm_group.add_argument(
        "--create-table-prefix",
        default="",
        help="DmBinding.create_table_prefix (default: '').",
    )

    parser.add_argument(
        "--deployment-id",
        default="",
        help="Optional deployment identifier propagated through the run.",
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

    files = _collect_files(
        explicit=list(args.files),
        manifest=args.files_from,
    )
    if not files:
        logger.error(
            "No files supplied; pass one or more --file / --files-from.",
        )
        return 2

    if args.mode == "dm" and not args.target_context:
        logger.error("--mode dm requires --target-context.")
        return 2

    return _dispatch(args=args, files=files)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


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


def _dispatch(*, args: argparse.Namespace, files: list[str]) -> int:
    import os

    from unity.common.pipeline import DispatchTarget, publish_parse_request
    from unity.common.pipeline.types import DmBinding, FmBinding
    from unity_deploy.infra.gcp.settings import GcpPipelineSettings

    settings = GcpPipelineSettings()
    project_id = settings.pubsub.project_id
    bucket_name = settings.artifact_store.bucket
    if not project_id:
        logger.error("UNITY_PUBSUB_PROJECT_ID is not set; cannot dispatch.")
        return 2
    if not bucket_name:
        logger.error("UNITY_GCS_ARTIFACT_BUCKET is not set; cannot dispatch.")
        return 2

    target = DispatchTarget(
        project_id=project_id,
        bucket_name=bucket_name,
        env_suffix=settings.env_suffix(),
        upload_prefix="dispatch/manual",
    )

    if args.mode == "fm":
        user_id = args.user_id or os.environ.get("USER_ID", "default")
        assistant_id = args.assistant_id or os.environ.get("ASSISTANT_ID", "0")
        logger.info(
            "=== Dispatch [mode=fm, env=%s, user_id=%s, assistant_id=%s, alias=%s] ===",
            settings.environment,
            user_id,
            assistant_id,
            args.alias,
        )
    else:
        logger.info(
            "=== Dispatch [mode=dm, env=%s, target_context=%s] ===",
            settings.environment,
            args.target_context,
        )

    logger.info("Dispatching %d file(s)...", len(files))

    errors = 0
    for path_or_uri in files:
        fm_binding: Optional[FmBinding] = None
        dm_binding: Optional[DmBinding] = None
        if args.mode == "fm":
            fm_binding = FmBinding(
                user_id=user_id,  # type: ignore[possibly-undefined]
                assistant_id=assistant_id,  # type: ignore[possibly-undefined]
                fm_alias=args.alias,
                logical_path=path_or_uri,
            )
        else:
            dm_binding = DmBinding(
                target_context=args.target_context,
                create_table_prefix=args.create_table_prefix,
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
                deployment_id=args.deployment_id,
                **source_kwargs,
            )
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

    logger.info(
        "=== Dispatch Complete (files=%d, errors=%d) ===",
        len(files),
        errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
