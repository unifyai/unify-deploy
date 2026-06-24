#!/usr/bin/env python3
"""Reconcile the self-host (localhost) Twilio numbers' inbound webhooks.

Text (SMS/WhatsApp) is always **poll-only**: ``comms_ingress_bridge.py`` polls
Twilio and forwards inbound to the local CM, so the messaging webhooks must stay
cleared — otherwise a hosted backend (e.g. staging adapters) answers the same
inbound synchronously (the "This number is no longer active" decommission reply),
because the local-only assistant/route does not exist in the hosted database.

Calls are different: a call is synchronous (Twilio POSTs the number's voice URL
and needs TwiML back in seconds), so they cannot be polled. When call support is
enabled, the localhost voice number's ``VoiceUrl`` must instead point at the
local CM ingress through the cloudflared tunnel, while the messaging webhook
stays cleared. Because a number has a single ``VoiceUrl``, voice is
single-owner-at-a-time across developers; ``stack down`` reverts it.

Modes:
  * default (no flag): keep messaging poll-only and, unless
    ``SELF_HOST_CALLS_ENABLED=0`` is set, point the voice webhook at the local
    tunnel.
  * ``--set-voice``: keep messaging poll-only, but point the voice webhook at the
    tunnel (``UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL``/``LOCAL_COMMS_PUBLIC_URL``).
  * ``--revert-voice``: clear the voice webhook back to poll-only (messaging was
    already cleared). Used by ``stack down``.
  * ``--check``: report drift without mutating; exit 1 on drift. Honors
    ``--set-voice`` (verifies voice points at the tunnel) vs poll-only otherwise.

The numbers are the git-tracked source of truth in ``self_host_env.sh`` and are
read from the environment (source that file, or run via ``stack.sh sync-comms``).
Twilio credentials are read from the environment or, as a convenience, from
``~/.unity/comms_twilio.env`` (secrets, never committed).

Channels:
  * WhatsApp — Twilio Senders v2 API, WhatsApp sub-account
    (``TWILIO_WA_ACCOUNT_SID`` / ``TWILIO_WA_AUTH_TOKEN``).
  * SMS / voice — Twilio IncomingPhoneNumbers API, main account
    (``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``).

Usage:
  python3 sync_comms_webhooks.py                  # voice -> tunnel, text poll-only
  python3 sync_comms_webhooks.py --set-voice       # same, explicit
  python3 sync_comms_webhooks.py --revert-voice    # clear voice back to poll-only
  python3 sync_comms_webhooks.py --check           # report drift, exit 1 if any
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
        Path(os.environ.get("UNITY_HOME", str(Path.home() / ".unity")))
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


def reconcile_whatsapp(
    number: str,
    env: dict,
    *,
    mode: str = "clear",
    public_url: str = "",
    check: bool = False,
) -> bool:
    """Reconcile the WhatsApp Sender's messaging webhook to poll-only.

    WhatsApp text is always poll-only (the bridge polls), so the Sender's
    messaging webhook stays cleared in every mode. WhatsApp Business *Calling*
    (voice) is handled separately via a TwiML Voice Application attached to the
    Sender; see ``reconcile_whatsapp_voice``.
    """
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
# WhatsApp Business Calling (TwiML Voice Application attached to the Sender)
# --------------------------------------------------------------------------

_WA_VOICE_APP_FRIENDLY_NAME = "Unity Self-Host WhatsApp Calls"


def _wa_voice_target(public_url: str) -> str:
    return f"{public_url}/local/twilio/whatsapp-call"


def _find_application(
    account_sid: str,
    headers: dict,
    friendly_name: str,
) -> dict | None:
    qs = urllib.parse.urlencode({"FriendlyName": friendly_name, "PageSize": "50"})
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/Applications.json?{qs}"
    )
    data = _request("GET", url, headers)
    apps = data.get("applications", [])
    return apps[0] if apps else None


def _ensure_application(
    account_sid: str,
    headers: dict,
    friendly_name: str,
    voice_url: str,
) -> str:
    """Create or update the TwiML Voice App and return its SID."""
    app = _find_application(account_sid, headers, friendly_name)
    post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Applications"
    if app is None:
        form = urllib.parse.urlencode(
            {
                "FriendlyName": friendly_name,
                "VoiceUrl": voice_url,
                "VoiceMethod": "POST",
            },
        ).encode()
        created = _request("POST", f"{base}.json", post_headers, data=form)
        return created.get("sid", "")
    sid = app["sid"]
    if (app.get("voice_url") or "") != voice_url:
        form = urllib.parse.urlencode(
            {"VoiceUrl": voice_url, "VoiceMethod": "POST"},
        ).encode()
        _request("POST", f"{base}/{sid}.json", post_headers, data=form)
    return sid


def _get_whatsapp_sender(sender_sid: str, headers: dict) -> dict:
    return _request("GET", f"{_SENDER_BASE}/{sender_sid}", headers)


def _sender_voice_app(sender: dict) -> str:
    config = sender.get("configuration") or {}
    return config.get("voice_application_sid") or ""


def _set_sender_voice_app(sender_sid: str, headers: dict, app_sid: str) -> None:
    body = json.dumps(
        {"configuration": {"voice_application_sid": app_sid}},
    ).encode()
    post_headers = {**headers, "Content-Type": "application/json"}
    _request("POST", f"{_SENDER_BASE}/{sender_sid}", post_headers, data=body)


def reconcile_whatsapp_voice(
    number: str,
    env: dict,
    *,
    mode: str,
    public_url: str,
    check: bool,
) -> bool:
    """Reconcile WhatsApp Business Calling for the localhost Sender.

    set-voice  -> ensure a TwiML Voice App (VoiceUrl=tunnel/local/twilio/whatsapp-call)
                  is attached to the Sender.
    clear/revert-voice -> detach the voice app.

    Best-effort: WhatsApp Business Calling must be enabled on the account, so
    Twilio API failures here are reported as warnings and never abort the phone
    reconciliation.
    """
    account_sid = env.get("TWILIO_WA_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_WA_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        return True
    headers = _auth_header(account_sid, auth_token)
    sender = _find_whatsapp_sender(number, headers)
    if sender is None:
        return True
    sender_sid = sender["sid"]
    try:
        full = _get_whatsapp_sender(sender_sid, headers)
    except urllib.error.HTTPError:
        full = sender
    cur_app = _sender_voice_app(full)

    if mode == "set-voice":
        want_voice = _wa_voice_target(public_url)
        if check:
            if cur_app:
                print(f"[whatsapp-call] {number}: voice app attached ✓")
                return True
            print(
                f"[whatsapp-call] {number}: DRIFT — no voice app attached "
                f"(want {want_voice})",
            )
            return False
        try:
            app_sid = _ensure_application(
                account_sid,
                headers,
                _WA_VOICE_APP_FRIENDLY_NAME,
                want_voice,
            )
            if app_sid and cur_app != app_sid:
                _set_sender_voice_app(sender_sid, headers, app_sid)
        except urllib.error.HTTPError as exc:
            print(
                f"[whatsapp-call] {number}: WARN — could not attach voice app "
                f"({exc.code}); is WhatsApp Business Calling enabled?",
            )
            return True
        print(f"[whatsapp-call] {number}: voice app {app_sid} → {want_voice} ✓")
        return True

    # clear / revert-voice → detach the voice app.
    if not cur_app:
        print(f"[whatsapp-call] {number}: no voice app (poll-only) ✓")
        return True
    if check:
        print(f"[whatsapp-call] {number}: DRIFT — voice app {cur_app} still attached")
        return False
    try:
        _set_sender_voice_app(sender_sid, headers, "")
    except urllib.error.HTTPError as exc:
        print(
            f"[whatsapp-call] {number}: WARN — could not detach voice app ({exc.code})",
        )
        return True
    print(f"[whatsapp-call] {number}: detached voice app → poll-only ✓")
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


def _public_url(env: dict) -> str:
    url = (
        env.get("UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL")
        or env.get("LOCAL_COMMS_PUBLIC_URL")
        or ""
    ).strip()
    return url.rstrip("/")


def _calls_enabled(env: dict) -> bool:
    return (env.get("SELF_HOST_CALLS_ENABLED") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _phone_voice_target(public_url: str) -> str:
    return f"{public_url}/local/twilio/call"


def _phone_status_target(public_url: str) -> str:
    return f"{public_url}/local/twilio/call-status"


def _update_incoming_number(
    account_sid: str,
    sid: str,
    headers: dict,
    fields: dict[str, str],
) -> None:
    form = urllib.parse.urlencode(fields).encode()
    post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/IncomingPhoneNumbers/{sid}.json"
    )
    _request("POST", url, post_headers, data=form)


def reconcile_phone(
    number: str,
    env: dict,
    *,
    mode: str,
    public_url: str,
    check: bool,
) -> bool:
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

    if mode == "set-voice":
        want_voice = _phone_voice_target(public_url)
        want_status = _phone_status_target(public_url)
        cur_voice = record.get("voice_url") or ""
        cur_sms = record.get("sms_url") or ""
        if cur_voice == want_voice and not cur_sms:
            print(
                f"[sms/voice] {number}: voice already → {want_voice} (text poll-only) ✓",
            )
            return True
        if check:
            print(
                f"[sms/voice] {number}: DRIFT — voice_url={cur_voice!r} "
                f"(want {want_voice!r}), sms_url={cur_sms!r} (want '')",
            )
            return False
        _update_incoming_number(
            account_sid,
            record["sid"],
            headers,
            {
                "VoiceUrl": want_voice,
                "VoiceMethod": "POST",
                "StatusCallback": want_status,
                "StatusCallbackMethod": "POST",
                "SmsUrl": "",
            },
        )
        print(f"[sms/voice] {number}: voice → {want_voice}, text poll-only ✓")
        return True

    # mode in {"clear", "revert-voice"} → full poll-only (no voice, no sms).
    if _phone_is_poll_only(record):
        print(f"[sms/voice] {number}: already poll-only ✓")
        return True
    if check:
        print(
            f"[sms/voice] {number}: DRIFT — sms_url={record.get('sms_url')!r} "
            f"voice_url={record.get('voice_url')!r}",
        )
        return False
    _update_incoming_number(
        account_sid,
        record["sid"],
        headers,
        {"SmsUrl": "", "VoiceUrl": "", "StatusCallback": ""},
    )
    print(f"[sms/voice] {number}: cleared sms_url + voice_url → poll-only ✓")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report drift without mutating; exit 1 if any localhost webhook drifted.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--set-voice",
        action="store_true",
        help="Point the voice webhook at the local tunnel (text stays poll-only).",
    )
    mode_group.add_argument(
        "--revert-voice",
        action="store_true",
        help="Clear the voice webhook back to poll-only (used by stack down).",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    _load_twilio_env_file(env)

    if args.set_voice:
        mode = "set-voice"
    elif args.revert_voice:
        mode = "revert-voice"
    elif _calls_enabled(env):
        mode = "set-voice"
    else:
        mode = "clear"

    public_url = _public_url(env)
    if mode == "set-voice" and not public_url:
        print(
            "ERROR: --set-voice needs UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL "
            "(the cloudflared tunnel URL). Is the call tunnel up?",
            file=sys.stderr,
        )
        return 2

    whatsapp_number = (
        env.get("COMMS_BRIDGE_WHATSAPP_NUMBER")
        or env.get("UNITY_COORDINATOR_WHATSAPP_NUMBER")
        or ""
    ).strip()
    phone_number = (
        env.get("COMMS_BRIDGE_SMS_NUMBER")
        or env.get("UNITY_COORDINATOR_PHONE")
        or env.get("UNITY_COORDINATOR_PHONE_US")
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
            ok &= reconcile_whatsapp(
                whatsapp_number,
                env,
                mode=mode,
                public_url=public_url,
                check=args.check,
            )
            ok &= reconcile_whatsapp_voice(
                whatsapp_number,
                env,
                mode=mode,
                public_url=public_url,
                check=args.check,
            )
        if phone_number:
            ok &= reconcile_phone(
                phone_number,
                env,
                mode=mode,
                public_url=public_url,
                check=args.check,
            )
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
