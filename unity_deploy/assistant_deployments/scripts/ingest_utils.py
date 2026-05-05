"""Shared utilities for client ingestion scripts.

Provides environment bootstrapping, project activation, config loading,
and progress reporter creation used by both ``ingest_fm.py`` and
``ingest_dm.py``.  Client-agnostic — the ``--client`` / ``--deployment``
arguments determine which config paths are resolved.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from unity_deploy.assistant_deployments.types.pipeline_config import PipelineConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_project_root() -> Path:
    """Walk up from this file until ``pyproject.toml`` is found."""
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    raise FileNotFoundError(
        "Could not locate project root (no pyproject.toml found "
        f"above {Path(__file__).resolve()})",
    )


def _clients_dir() -> Path:
    """Absolute path to ``unity_deploy/assistant_deployments/clients/``."""
    return Path(__file__).resolve().parent.parent / "clients"


def resolve_client_dir(client_name: str) -> Path:
    """Resolve and validate a client directory under ``clients/``.

    Raises :class:`FileNotFoundError` if the directory doesn't exist.
    """
    client_dir = _clients_dir() / client_name
    if not client_dir.is_dir():
        raise FileNotFoundError(
            f"Client '{client_name}' not found at {client_dir}. "
            f"Available: {sorted(d.name for d in _clients_dir().iterdir() if d.is_dir())}",
        )
    return client_dir


def default_pipeline_config_path(client: str, deployment: str = "v1") -> str:
    """Path to ``pipeline_config.json`` for a given client + deployment."""
    return str(
        resolve_client_dir(client)
        / "deployments"
        / deployment
        / "data"
        / "pipeline_config.json",
    )


# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------

_SDK_NOISE_PREFIXES = (
    "unify_requests",
    "unify",
    "unillm",
    "UnifyAsyncLogger",
    "httpx",
    "httpcore",
    "urllib3",
)


def initialize_environment(
    *,
    debug: bool = False,
    log_file: Optional[str] = None,
    sdk_log: bool = True,
) -> Path:
    """Load ``.env``, configure logging, and add project root to ``sys.path``.

    Logging behaviour:

    * **Terminal** -- app-level logs only; SDK/HTTP noise suppressed.
    * **Log file** (optional) -- receives everything.  SDK noise goes to a
      companion ``*_unify.log`` file when *sdk_log* is True.

    Returns the resolved project root path.
    """
    project_root = get_project_root()

    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    env_file = project_root / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(str(env_file), override=False)
        except ImportError:
            pass
    level = logging.DEBUG if debug else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    class _SuppressSDKNoise(logging.Filter):
        """Allow SDK loggers through only at WARNING+."""

        def filter(self, record: logging.LogRecord) -> bool:
            name = record.name or ""
            if any(name.startswith(p) for p in _SDK_NOISE_PREFIXES):
                return record.levelno >= logging.WARNING
            return True

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(_SuppressSDKNoise())
    root_logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(_SuppressSDKNoise())
        root_logger.addHandler(fh)

        if sdk_log:
            stem = Path(log_file).stem
            parent = Path(log_file).parent
            unify_log = str(parent / f"{stem}_unify.log")
            fh_sdk = logging.FileHandler(unify_log, mode="w", encoding="utf-8")
            fh_sdk.setFormatter(fmt)
            root_logger.addHandler(fh_sdk)
            print(f"  Unify/SDK logs -> {unify_log}")

    return project_root


# ---------------------------------------------------------------------------
# Project activation
# ---------------------------------------------------------------------------


def activate_project(
    project_name: str,
    *,
    overwrite: bool = False,
) -> None:
    """Activate a Unify project for ingestion.

    Calls ``unity.init()`` and optionally deletes/recreates the project.
    """
    if overwrite:
        try:
            from unify import delete_project

            delete_project(project_name)
            logger.info("Deleted existing project '%s'", project_name)
        except Exception:
            pass

    os.environ.setdefault("UNIFY_PROJECT_NAME", project_name)

    from unity.session_details import SESSION_DETAILS

    SESSION_DETAILS.populate_from_env()

    import unity

    unity.init(project_name=project_name, overwrite=overwrite)
    logger.info(
        "Activated project '%s' (context: %s/%s)",
        project_name,
        SESSION_DETAILS.user_context,
        SESSION_DETAILS.assistant_context,
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_pipeline_config(
    config_path: str,
    *,
    project_root: Optional[Path] = None,
) -> "PipelineConfig":
    """Load, validate, and resolve the shared pipeline JSON config.

    Parameters
    ----------
    config_path : str
        Path to the JSON file.
    project_root : Path | None
        Project root for resolving relative ``file_path`` entries.
        Defaults to ``get_project_root()``.

    Returns
    -------
    PipelineConfig
        A validated, path-resolved configuration object.
    """
    from unity_deploy.assistant_deployments.types.pipeline_config import PipelineConfig

    config = PipelineConfig.from_file(config_path)

    root = project_root or get_project_root()
    config.resolve_paths(root)

    logger.info("Loaded pipeline config from %s", config_path)
    return config


# ---------------------------------------------------------------------------
# Run directory & progress reporter
# ---------------------------------------------------------------------------


def create_run_directory(
    script_name: str,
    *,
    base_dir: Optional[str] = None,
) -> Path:
    """Create and return a timestamped run directory.

    Layout::

        <base_dir>/<script_name>/<YYYY-MM-DDTHH-MM-SS>/
            progress.jsonl      (created by reporter)
            error_details/      (created on first error)
    """
    import time as _time

    root = Path(base_dir) if base_dir else Path("logs") / "pipeline"
    ts = _time.strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = root / script_name / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def create_pipeline_reporter(
    diagnostics: Dict[str, Any],
    script_name: str,
    *,
    run_dir: Optional[Path] = None,
    progress_file_override: Optional[str] = None,
    verbosity_override: Optional[str] = None,
):
    """Create a composite ProgressReporter from the diagnostics config section.

    Returns a ``(reporter, run_dir)`` tuple.
    """
    from unity.file_manager.managers.utils.progress import (
        CompositeReporter,
        ConsoleReporter,
        NoOpReporter,
        create_reporter,
    )

    enable = diagnostics.get("enable_progress", False)
    if not enable:
        return NoOpReporter(), run_dir

    if run_dir is None:
        run_dir = create_run_directory(script_name)

    mode = diagnostics.get("progress_mode", "json_file")
    verbosity = verbosity_override or diagnostics.get("verbosity", "medium")

    if progress_file_override:
        progress_file = progress_file_override
        error_dir = str(Path(progress_file_override).parent / "error_details")
    else:
        progress_file = str(run_dir / "progress.jsonl")
        error_dir = str(run_dir / "error_details")

    json_reporter = create_reporter(
        mode=mode,
        file_path=progress_file,
        verbosity=verbosity,
        error_dir=error_dir,
    )
    console_reporter = ConsoleReporter(emoji=False, show_timestamps=True)

    logger.info("Run directory: %s", run_dir)
    logger.info("Progress file: %s (verbosity=%s)", progress_file, verbosity)
    return CompositeReporter([json_reporter, console_reporter]), run_dir


# ---------------------------------------------------------------------------
# Config merging
# ---------------------------------------------------------------------------


def merge_pipeline_configs(
    config_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    global_rules: List[str] | None = None,
) -> "PipelineConfig":
    """Merge individual per-table pipeline configs into one consolidated config.

    Reads every ``*.json`` file in *config_dir*, validates each as a
    :class:`PipelineConfig`, then produces a single merged config.
    """
    from unity_deploy.assistant_deployments.types.pipeline_config import PipelineConfig

    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise FileNotFoundError(
            f"Pipeline config directory does not exist or is not a directory: "
            f"{config_dir.resolve()}",
        )
    json_files = sorted(config_dir.rglob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No *.json files found in {config_dir.resolve()}")

    configs: list[PipelineConfig] = []
    for p in json_files:
        configs.append(PipelineConfig.from_file(str(p)))
    logger.info(
        "Loaded %d individual pipeline configs from %s",
        len(configs),
        config_dir,
    )

    first = configs[0]

    all_source_files: list[dict] = []
    all_file_contexts: list[dict] = []
    seen_global_rules: dict[str, None] = {}
    all_embed_specs: list[dict] = []
    seen_derived: list[dict] = []

    for cfg in configs:
        for sf in cfg.source_files:
            all_source_files.append(sf.model_dump())

        if cfg.ingest.business_contexts:
            for rule in cfg.ingest.business_contexts.global_rules:
                seen_global_rules.setdefault(rule, None)
            for fc in cfg.ingest.business_contexts.file_contexts:
                all_file_contexts.append(fc.model_dump())

        for dc in cfg.post_ingest.derived_columns:
            dc_dict = dc.model_dump()
            if dc_dict not in seen_derived:
                seen_derived.append(dc_dict)

        if cfg.embed.strategy != "off" and cfg.embed.file_specs:
            for fs in cfg.embed.file_specs:
                all_embed_specs.append(fs.model_dump())

    merged_global_rules = list(global_rules or [])
    for rule in seen_global_rules:
        if rule not in merged_global_rules:
            merged_global_rules.append(rule)

    has_embed = len(all_embed_specs) > 0

    merged_dict: dict[str, Any] = {
        "source_files": all_source_files,
        "parse": first.parse.model_dump(),
        "ingest": {
            "table_rows_batch_size": first.ingest.table_rows_batch_size,
            "content_rows_batch_size": first.ingest.content_rows_batch_size,
            "infer_untyped_fields": first.ingest.infer_untyped_fields,
            "business_contexts": {
                "global_rules": merged_global_rules,
                "file_contexts": all_file_contexts,
            },
        },
        "post_ingest": {"derived_columns": seen_derived},
        "embed": {
            "strategy": "along" if has_embed else "off",
            "file_specs": all_embed_specs,
        },
        "output": first.output.model_dump(),
        "execution": first.execution.model_dump(),
        "retry": first.retry.model_dump(),
        "diagnostics": first.diagnostics.model_dump(),
    }

    merged = PipelineConfig.model_validate(merged_dict)
    logger.info(
        "Merged config: %d source files, %d file contexts, %d embed specs, %d derived rules",
        len(all_source_files),
        len(all_file_contexts),
        len(all_embed_specs),
        len(seen_derived),
    )

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(merged_dict, f, indent=2, ensure_ascii=False)
            f.write("\n")
        logger.info("Wrote merged pipeline config to %s", output_path)

    return merged
