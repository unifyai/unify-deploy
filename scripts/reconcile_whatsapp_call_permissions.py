#!/usr/bin/env python3
"""Reconcile WhatsApp call-permission callbacks from Twilio message history."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import requests
from twilio.rest import Client


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _strip_whatsapp(number: str) -> str:
    return number.replace("whatsapp:", "").strip()


def _permission_status(button_payload: str) -> str:
    payload = (button_payload or "").strip()
    if payload == "ACCEPTED":
        return "accepted"
    if payload == "REJECTED":
        return "rejected"
    return "unknown_interaction"


def _twilio_client() -> Client:
    sid = _env("TWILIO_WA_ACCOUNT_SID") or _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_WA_AUTH_TOKEN") or _env("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise RuntimeError("Twilio credentials are missing.")
    return Client(sid, token)


def _post_permission(
    *,
    orchestra_url: str,
    admin_key: str,
    pool_number: str,
    contact_number: str,
    status: str,
) -> None:
    response = requests.post(
        f"{orchestra_url.rstrip('/')}/admin/whatsapp/call-permission",
        headers={"Authorization": f"Bearer {admin_key}"},
        json={
            "pool_number": pool_number,
            "contact_number": contact_number,
            "status": status,
            "source": "twilio_reconciliation",
        },
        timeout=10,
    )
    response.raise_for_status()


def reconcile(args: argparse.Namespace) -> int:
    orchestra_url = args.orchestra_url or _env("ORCHESTRA_URL")
    admin_key = args.orchestra_admin_key or _env("ORCHESTRA_ADMIN_KEY")
    if not orchestra_url or not admin_key:
        raise RuntimeError("ORCHESTRA_URL and ORCHESTRA_ADMIN_KEY are required.")

    client = _twilio_client()
    since_dt = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)
    reconciled = 0

    for pool_number in args.pool_number:
        messages = client.messages.list(
            to=f"whatsapp:{pool_number}",
            date_sent_after=since_dt,
            limit=args.limit,
        )
        for msg in messages:
            if getattr(msg, "direction", "") != "inbound":
                continue
            if (getattr(msg, "body", "") or "").strip() != "VOICE_CALL_REQUEST":
                continue
            contact_number = _strip_whatsapp(msg.from_ or "")
            status = _permission_status(
                getattr(msg, "button_payload", "") or getattr(msg, "ButtonPayload", ""),
            )
            print(
                "reconcile whatsapp_call_permission "
                f"pool={pool_number} contact={contact_number} status={status}",
                flush=True,
            )
            if not args.dry_run:
                _post_permission(
                    orchestra_url=orchestra_url,
                    admin_key=admin_key,
                    pool_number=pool_number,
                    contact_number=contact_number,
                    status=status,
                )
            reconciled += 1

    print(f"reconciled={reconciled} dry_run={args.dry_run}", flush=True)
    return reconciled


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile Twilio WhatsApp call-permission responses into Orchestra.",
    )
    parser.add_argument(
        "--pool-number",
        action="append",
        required=True,
        help="WhatsApp pool number in E.164 format. May be passed more than once.",
    )
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--orchestra-url", default="")
    parser.add_argument("--orchestra-admin-key", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    reconcile(parse_args())
