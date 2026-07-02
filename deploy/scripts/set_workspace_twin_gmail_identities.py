#!/usr/bin/env python3
"""Sync Workspace + Gmail identity fields for platform T-W1N mailboxes.

Updates:
- Admin SDK Directory ``name`` fields (given/family/full)
- Gmail ``sendAs`` display names (requires ``gmail.settings.basic`` DWD)

Requires ``~/.unity/comms_sa.json`` (or ``GCP_SA_KEY`` / ``GMAIL_BRIDGE_SA_FILE``)
with domain-wide delegation for the scopes listed in ``README.md`` § comm-sa DWD.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build

TWIN_DISPLAY_NAME = "T-W1N"
DEFAULT_TWIN_EMAILS = (
    "twin@unify.ai",
    "staging-twin@unify.ai",
    "local-twin@unify.ai",
)
DIRECTORY_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user"]
GMAIL_SETTINGS_SCOPES = ["https://www.googleapis.com/auth/gmail.settings.basic"]
WORKSPACE_ADMIN_SUBJECT = os.environ.get("WORKSPACE_ADMIN_SUBJECT", "dan@unify.ai")


def _sa_file() -> Path:
    for candidate in (
        os.environ.get("GCP_SA_KEY_FILE"),
        os.environ.get("GMAIL_BRIDGE_SA_FILE"),
        str(Path.home() / ".unity" / "comms_sa.json"),
    ):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit(
        "No service-account JSON found. Set GCP_SA_KEY_FILE or place key at ~/.unity/comms_sa.json",
    )


def _load_sa_info() -> dict:
    return json.loads(_sa_file().read_text())


def _delegated_creds(info: dict, *, scopes: list[str], subject: str):
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=scopes,
        subject=subject,
    )
    creds.refresh(Request())
    return creds


def _update_directory_name(admin, email: str) -> None:
    admin.users().update(
        userKey=email,
        body={
            "name": {
                "givenName": TWIN_DISPLAY_NAME,
                "familyName": "",
            },
        },
    ).execute()


def _update_send_as_display_name(gmail, email: str) -> None:
    gmail.users().settings().sendAs().patch(
        userId="me",
        sendAsEmail=email,
        body={"displayName": TWIN_DISPLAY_NAME},
    ).execute()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "emails",
        nargs="*",
        default=list(DEFAULT_TWIN_EMAILS),
        help="Twin mailbox addresses to update (default: prod/staging/local)",
    )
    parser.add_argument(
        "--skip-send-as",
        action="store_true",
        help="Only update Admin Directory names (skip Gmail sendAs)",
    )
    args = parser.parse_args()

    info = _load_sa_info()
    admin = build(
        "admin",
        "directory_v1",
        credentials=_delegated_creds(
            info,
            scopes=DIRECTORY_SCOPES,
            subject=WORKSPACE_ADMIN_SUBJECT,
        ),
    )

    for email in args.emails:
        _update_directory_name(admin, email)
        print(f"Directory name set for {email}")

    if args.skip_send_as:
        return 0

    send_as_errors: list[str] = []
    for email in args.emails:
        try:
            gmail = build(
                "gmail",
                "v1",
                credentials=_delegated_creds(
                    info,
                    scopes=GMAIL_SETTINGS_SCOPES,
                    subject=email,
                ),
            )
            _update_send_as_display_name(gmail, email)
            print(f"Gmail sendAs displayName set for {email}")
        except Exception as exc:
            send_as_errors.append(f"{email}: {exc}")

    if send_as_errors:
        print(
            "\nGmail sendAs updates failed. Ensure comm-sa domain-wide delegation "
            "includes https://www.googleapis.com/auth/gmail.settings.basic "
            "(see README.md § comm-sa Workspace domain-wide delegation), wait a "
            "few minutes, then re-run this script.",
            file=sys.stderr,
        )
        for line in send_as_errors:
            print(f"  - {line}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
