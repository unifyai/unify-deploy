#!/usr/bin/env python3
"""Reconcile the self-host (localhost) Twilio numbers to "poll-only".

Self-host has no public webhook: ``comms_ingress_bridge.py`` polls Twilio for
inbound SMS/WhatsApp and forwards items to the local CM. For that model to work
cleanly, the localhost numbers must NOT have a hosted inbound webhook attached —
otherwise a hosted backend (e.g. staging adapters) receives the same inbound
synchronously and answers it (typically with the "This number is no longer
active" decommission reply), because the local-only assistant/route does not
exist in the hosted database.

This script enforces that invariant: it clears the inbound (and status) webhooks
on the localhost WhatsApp Sender and the localhost SMS/voice number, so the
polling bridge is the sole consumer. It is idempotent — numbers already cleared
are left untouched.

The numbers are the git-tracked source of truth in ``self_host_env.sh`` and are
read from the environment (source that file, or run via ``stack.sh sync-comms``).
Twilio credentials are read from the environment or, as a convenience, from
``~/.droid/comms_twilio.env`` (secrets, never committed).

Channels:
  * WhatsApp — Twilio Senders v2 API, WhatsApp sub-account
    (``TWILIO_WA_ACCOUNT_SID`` / ``TWILIO_WA_AUTH_TOKEN``).
  * SMS / voice — Twilio IncomingPhoneNumbers API, main account
    (``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``).

Usage:
  python3 sync_comms_webhooks.py            # apply (clear webhooks)
  python3 sync_comms_webhooks.py --check     # report drift only, exit 1 if any
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_TWILIO_ENV_FILE = Path(
    os.environ.get(
        "SELF_HOST_COMMS_TWILIO_FILE",
        Path(os.environ.get("DROID_HOME", str(Path.home() / ".droid")))
        / "comms_twilio.env",
    ),
)


def _load_twilio_env_file(env: dict[str, str]) -> None:
    """Backfill missing Twilio keys from the local secrets file, if present."""
    if not _TWILIO_ENV_FILE.is_file():
        return
    for raw in _TWILIO_ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        env.setdefault(key, val)


def _auth_header(account_sid: str, auth_token: str) -> dict[str, str]:
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _request(method: str, url: str, headers: dict, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


# --------------------------------------------------------------------------
# WhatsApp (Senders v2)
# --------------------------------------------------------------------------

_SENDER_BASE = "https://messaging.twilio.com/v2/Channels/Senders"


def _find_whatsapp_sender(number: str, headers: dict) -> dict | None:
    url = f"{_SENDER_BASE}?Channel=whatsapp&PageSize=100"
    data = _request("GET", url, headers)
    target = f"whatsapp:{number}"
    for sender in data.get("senders", []):
        if sender.get("sender_id") == target:
            return sender
    return None


def _whatsapp_is_poll_only(sender: dict) -> bool:
    webhook = sender.get("webhook") or {}
    return not webhook.get("callback_url") and not webhook.get("status_callback_url")


def reconcile_whatsapp(number: str, env: dict, *, check: bool) -> bool:
    """Return True if already poll-only (or made so); False on drift in check mode."""
    account_sid = env.get("TWILIO_WA_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_WA_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        print("[whatsapp] SKIP — TWILIO_WA_ACCOUNT_SID/TWILIO_WA_AUTH_TOKEN not set")
        return True
    headers = _auth_header(account_sid, auth_token)
    sender = _find_whatsapp_sender(number, headers)
    if sender is None:
        print(f"[whatsapp] {number}: no Sender found (nothing to reconcile)")
        return True
    if _whatsapp_is_poll_only(sender):
        print(f"[whatsapp] {number}: already poll-only ✓")
        return True
    if check:
        webhook = sender.get("webhook") or {}
        print(
            f"[whatsapp] {number}: DRIFT — inbound webhook set to "
            f"{webhook.get('callback_url')!r}",
        )
        return False
    sender_sid = sender["sid"]
    body = json.dumps(
        {
            "webhook": {
                "callback_method": "POST",
                "callback_url": "",
                "status_callback_method": "POST",
                "status_callback_url": "",
            },
        },
    ).encode()
    post_headers = {**headers, "Content-Type": "application/json"}
    _request("POST", f"{_SENDER_BASE}/{sender_sid}", post_headers, data=body)
    print(f"[whatsapp] {number}: cleared inbound + status webhooks → poll-only ✓")
    return True


# --------------------------------------------------------------------------
# SMS / voice (IncomingPhoneNumbers)
# --------------------------------------------------------------------------


def _find_incoming_number(number: str, account_sid: str, headers: dict) -> dict | None:
    qs = urllib.parse.urlencode({"PhoneNumber": number})
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/IncomingPhoneNumbers.json?{qs}"
    )
    data = _request("GET", url, headers)
    numbers = data.get("incoming_phone_numbers", [])
    return numbers[0] if numbers else None


def _phone_is_poll_only(record: dict) -> bool:
    return not record.get("sms_url") and not record.get("voice_url")


def reconcile_phone(number: str, env: dict, *, check: bool) -> bool:
    account_sid = env.get("TWILIO_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        print("[sms/voice] SKIP — TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set")
        return True
    headers = _auth_header(account_sid, auth_token)
    record = _find_incoming_number(number, account_sid, headers)
    if record is None:
        print(
            f"[sms/voice] {number}: no IncomingPhoneNumber found (nothing to reconcile)",
        )
        return True
    if _phone_is_poll_only(record):
        print(f"[sms/voice] {number}: already poll-only ✓")
        return True
    if check:
        print(
            f"[sms/voice] {number}: DRIFT — sms_url={record.get('sms_url')!r} "
            f"voice_url={record.get('voice_url')!r}",
        )
        return False
    sid = record["sid"]
    form = urllib.parse.urlencode(
        {"SmsUrl": "", "VoiceUrl": "", "StatusCallback": ""},
    ).encode()
    post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/IncomingPhoneNumbers/{sid}.json"
    )
    _request("POST", url, post_headers, data=form)
    print(f"[sms/voice] {number}: cleared sms_url + voice_url → poll-only ✓")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report drift without mutating; exit 1 if any localhost number is not poll-only.",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    _load_twilio_env_file(env)

    whatsapp_number = (
        env.get("COMMS_BRIDGE_WHATSAPP_NUMBER")
        or env.get("DROID_COORDINATOR_WHATSAPP_NUMBER")
        or ""
    ).strip()
    phone_number = (
        env.get("COMMS_BRIDGE_SMS_NUMBER")
        or env.get("DROID_COORDINATOR_PHONE")
        or env.get("DROID_COORDINATOR_PHONE_US")
        or ""
    ).strip()

    if not whatsapp_number and not phone_number:
        print(
            "ERROR: no localhost numbers in env. Source selfhost/self_host_env.sh "
            "first, or run via 'stack.sh sync-comms'.",
            file=sys.stderr,
        )
        return 2

    ok = True
    try:
        if whatsapp_number:
            ok &= reconcile_whatsapp(whatsapp_number, env, check=args.check)
        if phone_number:
            ok &= reconcile_phone(phone_number, env, check=args.check)
    except urllib.error.HTTPError as exc:
        print(
            f"ERROR: Twilio API call failed: {exc.code} {exc.read().decode()[:300]}",
            file=sys.stderr,
        )
        return 2

    if args.check and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
