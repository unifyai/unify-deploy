"""Load the unity-deploy checkout ``.env`` without importing client packages.

Importing ``unify_deploy.assistant_deployments.clients.client_alpha`` (even to reach
``ingest_utils``) executes ``client_alpha/__init__.py``, which pulls in
``pipeline_config`` and thus ``unity`` → ``unify``.  ``unify`` binds
``BASE_URL`` from ``os.environ`` at import time, so ``.env`` must be loaded
**before** that chain runs.

Call :func:`load_repo_dotenv` at the very start of CLI entrypoints that use
package-qualified imports under ``unify_deploy.assistant_deployments.clients``.
"""

from __future__ import annotations

from pathlib import Path
import os
import logging

logger = logging.getLogger(__name__)


def unify_deploy_repo_root() -> Path:
    """Return the repository root (parent of the ``unify_deploy`` package)."""
    return Path(__file__).resolve().parents[2]


def drop_stale_google_application_credentials() -> None:
    """Unset GOOGLE_APPLICATION_CREDENTIALS when the file is missing.

    A stale path in ``.env`` (or injected by ``uv run``) makes
    ``google.auth.default()`` fail before gcloud / ADC can be used.
    """
    raw = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not raw:
        return
    path = Path(raw).expanduser()
    if path.is_file():
        return
    logger.warning(
        "GOOGLE_APPLICATION_CREDENTIALS=%s does not exist; unsetting so "
        "dispatch can use application-default / gcloud credentials.",
        raw,
    )
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def load_repo_dotenv(*, override: bool = False) -> Path:
    """Load ``<repo-root>/.env`` if the file exists.

    Parameters
    ----------
    override
        Passed to ``dotenv.load_dotenv``. Use ``True`` only when the process
        environment must be replaced by file values (e.g. conflicting exports).
    """
    root = unify_deploy_repo_root()
    env_path = root / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(str(env_path), override=override)
        except ImportError:
            pass
    drop_stale_google_application_credentials()
    return root
