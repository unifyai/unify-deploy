"""Bootstrap client bundles before offline task execution."""

from __future__ import annotations

import os


def main() -> None:
    from unity_deploy.client_bundle.fetch import (
        bundled_client_mode,
        ensure_client_bundle,
    )

    if not bundled_client_mode():
        return

    assistant_raw = (os.environ.get("ASSISTANT_ID") or "").strip()
    org_raw = (os.environ.get("ORG_ID") or "").strip()
    user_id = (os.environ.get("USER_ID") or "").strip() or None
    assistant_id = int(assistant_raw) if assistant_raw.isdigit() else None
    org_id = int(org_raw) if org_raw.isdigit() else None

    ensure_client_bundle(
        org_id=org_id,
        team_ids=None,
        user_id=user_id,
        assistant_id=assistant_id,
    )


if __name__ == "__main__":
    main()
