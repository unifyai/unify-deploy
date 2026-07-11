"""Bootstrap client bundles before offline task execution.

Offline Unity jobs do not run the live ConversationManager startup hook, so
they must fetch/unpack the GCS client bundle and register its import path
before ``@custom_function`` bodies can resolve
``unify_deploy.assistant_deployments.clients.<client>.*``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _parse_int_env(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw or not raw.isdigit():
        return None
    return int(raw)


def _parse_team_ids() -> list[int]:
    raw = (os.environ.get("TEAM_IDS") or "").strip()
    if not raw:
        return []
    team_ids: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if token.isdigit():
            team_ids.append(int(token))
    return team_ids


def ensure_offline_client_bundle() -> None:
    """Fetch, unpack, and register the assistant's client bundle when mapped.

    Delegates to ``resolve_startup_spec``, the same path live assistants use at
    wake: ensure the GCS bundle is present, then register
    ``unify_deploy.assistant_deployments.clients.<client>`` for imports.
    """

    from unify_deploy.client_bundle.fetch import bundled_client_mode
    from unify_deploy.startup_config import StartupIdentity, resolve_startup_spec

    if not bundled_client_mode():
        return

    assistant_id = _parse_int_env("ASSISTANT_ID")
    if assistant_id is None:
        logger.warning(
            "Skipping offline client-bundle bootstrap: ASSISTANT_ID is missing",
        )
        return

    resolve_startup_spec(
        StartupIdentity(
            assistant_id=str(assistant_id),
            user_id=(os.environ.get("USER_ID") or "").strip(),
            org_id=_parse_int_env("ORG_ID"),
            team_ids=tuple(_parse_team_ids()),
        ),
    )


def main() -> None:
    ensure_offline_client_bundle()


if __name__ == "__main__":
    main()
