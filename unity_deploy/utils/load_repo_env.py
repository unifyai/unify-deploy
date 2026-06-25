"""Load the unity-deploy checkout ``.env`` without importing client packages.

Importing ``unity_deploy.assistant_deployments.clients.client_alpha`` (even to reach
``ingest_utils``) executes ``client_alpha/__init__.py``, which pulls in
``pipeline_config`` and thus ``unity`` → ``unify``.  ``unify`` binds
``BASE_URL`` from ``os.environ`` at import time, so ``.env`` must be loaded
**before** that chain runs.

Call :func:`load_repo_dotenv` at the very start of CLI entrypoints that use
package-qualified imports under ``unity_deploy.assistant_deployments.clients``.
"""

from __future__ import annotations

from pathlib import Path


def unity_deploy_repo_root() -> Path:
    """Return the repository root (parent of the ``unity_deploy`` package)."""
    return Path(__file__).resolve().parents[2]


def load_repo_dotenv(*, override: bool = False) -> Path:
    """Load ``<repo-root>/.env`` if the file exists.

    Parameters
    ----------
    override
        Passed to ``dotenv.load_dotenv``. Use ``True`` only when the process
        environment must be replaced by file values (e.g. conflicting exports).
    """
    root = unity_deploy_repo_root()
    env_path = root / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(str(env_path), override=override)
        except ImportError:
            pass
    return root
