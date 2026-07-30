#!/usr/bin/env python3
"""Sync multiplayer twin alias identities onto the catch-all mailbox.

Every multiplayer twin owns an alias address on the twin alias domain
(e.g. ``max.vector@twins.unify.ai``) delivered into one shared Workspace
mailbox. Outbound sends set the From header per message (the runtime passes
the twin's current name as ``from_name``), but Gmail only honors a From it
recognises: this script registers a ``sendAs`` entry on the catch-all
account for each twin alias, carrying the twin's display name.

Run it after flips or renames (idempotent), or on a schedule:

    python3 deploy/scripts/sync_twin_alias_identities.py
    python3 deploy/scripts/sync_twin_alias_identities.py --check   # dry run

Reads multiplayer twins from Orchestra ``GET /admin/assistant``
(``ORCHESTRA_URL`` + ``ORCHESTRA_ADMIN_KEY``). Requires
``~/.unity/comms_sa.json`` (or ``GCP_SA_KEY_FILE`` / ``GMAIL_BRIDGE_SA_FILE``)
with domain-wide delegation for ``gmail.settings.basic``.

Limits worth knowing:
- Gmail caps sendAs entries per account (~30). Beyond that, sends still
  carry the right From header at the MIME level, but Gmail may rewrite the
  envelope; revisit the alias architecture before the cap is near.
- Avatars cannot be synced: Google resolves sender photos from real user
  accounts, and alias addresses have none. Twin appearance in recipients'
  inboxes is name-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SETTINGS_SCOPES = ["https://www.googleapis.com/auth/gmail.settings.sharing"]

ALIAS_MAILBOX = (
    (os.environ.get("UNITY_TWIN_ALIAS_MAILBOX") or "twins@unify.ai").strip().lower()
)
ALIAS_DOMAIN = (
    (os.environ.get("UNITY_TWIN_ALIAS_EMAIL_DOMAIN") or "twins.unify.ai")
    .strip()
    .lower()
)
ORCHESTRA_URL = (os.environ.get("ORCHESTRA_URL") or "https://api.unify.ai/v0").rstrip(
    "/",
)


def _sa_file() -> Path:
    for candidate in (
        os.environ.get("GCP_SA_KEY_FILE"),
        os.environ.get("GMAIL_BRIDGE_SA_FILE"),
        str(Path.home() / ".unity" / "comms_sa.json"),
    ):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit(
        "No service-account JSON found. Set GCP_SA_KEY_FILE or place key at "
        "~/.unity/comms_sa.json",
    )


def _delegated_gmail():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(_sa_file().read_text()),
        scopes=GMAIL_SETTINGS_SCOPES,
        subject=ALIAS_MAILBOX,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _multiplayer_twins() -> list[dict]:
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "").strip()
    if not admin_key:
        raise SystemExit("ORCHESTRA_ADMIN_KEY must be set to list assistants")
    resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"from_fields": "agent_id,first_name,surname,is_multiplayer,email"},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    if isinstance(rows, dict):
        rows = rows.get("info") or []
    twins = []
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if row.get("is_multiplayer") and email.endswith(f"@{ALIAS_DOMAIN}"):
            display_name = " ".join(
                part
                for part in ((row.get("first_name") or ""), (row.get("surname") or ""))
                if part.strip()
            ).strip()
            twins.append({"email": email, "display_name": display_name})
    return twins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing anything",
    )
    args = parser.parse_args()

    twins = _multiplayer_twins()
    if not twins:
        print("No multiplayer twins with alias addresses found; nothing to sync.")
        return 0

    gmail = _delegated_gmail()
    existing = {
        (entry.get("sendAsEmail") or "").strip().lower(): entry
        for entry in gmail.users()
        .settings()
        .sendAs()
        .list(userId="me")
        .execute()
        .get("sendAs", [])
    }

    failures: list[str] = []
    for twin in twins:
        email, display_name = twin["email"], twin["display_name"]
        current = existing.get(email)
        if current and (current.get("displayName") or "") == display_name:
            print(f"OK       {email} ({display_name})")
            continue
        action = "PATCH" if current else "CREATE"
        if args.check:
            print(f"{action}?   {email} ({display_name})")
            continue
        try:
            if current:
                gmail.users().settings().sendAs().patch(
                    userId="me",
                    sendAsEmail=email,
                    body={"displayName": display_name},
                ).execute()
            else:
                gmail.users().settings().sendAs().create(
                    userId="me",
                    body={
                        "sendAsEmail": email,
                        "displayName": display_name,
                        "treatAsAlias": True,
                    },
                ).execute()
            print(f"{action}    {email} ({display_name})")
        except HttpError as exc:
            failures.append(f"{email}: {exc}")

    if failures:
        print(
            "\nSome sendAs updates failed. Ensure comm-sa domain-wide delegation "
            "includes https://www.googleapis.com/auth/gmail.settings.sharing "
            "(sendAs create requires the sharing scope) and that the alias "
            "domain is verified in the Workspace tenant, then re-run.",
            file=sys.stderr,
        )
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
