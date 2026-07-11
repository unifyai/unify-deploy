#!/usr/bin/env python3
"""Reconcile the self-host (localhost) Twilio numbers' inbound webhooks.

Text (SMS/WhatsApp) is always **poll-only**: ``comms_ingress_bridge.py`` polls
Twilio and forwards inbound to the local CM, so the messaging webhooks must stay
cleared — otherwise a hosted backend (e.g. staging adapters) answers the same
inbound synchronously (the "This number is no longer active" decommission reply),
because the local-only assistant/route does not exist in the hosted database.

Calls are different: a call is synchronous (Twilio POSTs the number's voice URL
and needs TwiML back in seconds), so they cannot be polled. Acquisitions use a
persistent installation owner ID, tag public callbacks with that owner, and
snapshot prior state. Release restores only callbacks that still exactly match
what this installation wrote; it never clears another installation's state.

Modes:
  * default (no flag): keep messaging poll-only and, unless
    ``SELF_HOST_CALLS_ENABLED=0`` is set, point the voice webhook at the local
    tunnel.
  * ``--set-voice``: keep messaging poll-only, but point the voice webhook at the
    tunnel (``UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL``/``LOCAL_COMMS_PUBLIC_URL``).
  * ``--set-voice-only``: update only voice callbacks for tunnel rotation.
  * ``--revert-voice``: safely acquire poll-only voice state.
  * ``--acquire-text``: safely acquire poll-only messaging state.
  * ``--release``: compare-and-restore all state owned by this installation.
  * ``--release-text`` / ``--release-voice``: restore one owned callback scope.
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
  python3 sync_comms_webhooks.py --release         # restore captured prior state
  python3 sync_comms_webhooks.py --check           # report drift, exit 1 if any
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

_TWILIO_ENV_FILE = Path(
    os.environ.get(
        "SELF_HOST_COMMS_TWILIO_FILE",
        Path(os.environ.get("UNITY_HOME", str(Path.home() / ".unity")))
        / "comms_twilio.env",
    ),
)
_OWNER_QUERY_KEY = "unity_installation_owner"
_STATE_VERSION = 1
_ACTIVE_OWNER = ""
_ACTIVE_STATE: dict = {}


def _state_root() -> Path:
    return Path(
        os.environ.get(
            "SELF_HOST_STATE_DIR",
            os.environ.get("UNITY_HOME", str(Path.home() / ".unity")),
        ),
    )


def _owner_file() -> Path:
    return Path(
        os.environ.get(
            "SELF_HOST_INSTALLATION_OWNER_FILE",
            _state_root() / "installation-owner-id",
        ),
    )


def _state_file() -> Path:
    return Path(
        os.environ.get(
            "SELF_HOST_COMMS_WEBHOOK_STATE_FILE",
            _state_root() / "comms-webhook-state.json",
        ),
    )


def _lock_file() -> Path:
    return _state_root() / "comms-webhook-state.lock"


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent != Path("/runtime"):
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _installation_owner() -> str:
    path = _owner_file()
    try:
        owner = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        owner = ""
    if owner:
        return owner
    owner = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent != Path("/runtime"):
        path.parent.chmod(0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        persisted = path.read_text(encoding="utf-8").strip()
        if not persisted:
            raise RuntimeError("installation owner file is empty")
        return persisted
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{owner}\n")
    return owner


def _load_state(owner: str) -> dict:
    path = _state_file()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": _STATE_VERSION, "owner": owner, "resources": {}}
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"cannot read webhook ownership state: {exc}") from exc
    if state.get("owner") != owner:
        raise RuntimeError(
            "webhook ownership state belongs to a different installation "
            f"({state.get('owner')!r})",
        )
    if state.get("version") != _STATE_VERSION:
        raise RuntimeError("unsupported webhook ownership state version")
    state.setdefault("resources", {})
    return state


def _save_state(state: dict) -> None:
    resources = state.get("resources") or {}
    path = _state_file()
    if not resources:
        path.unlink(missing_ok=True)
        return
    _atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _tag_url(url: str, owner: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != _OWNER_QUERY_KEY]
    query.append((_OWNER_QUERY_KEY, owner))
    return urllib.parse.urlunsplit(
        parsed._replace(query=urllib.parse.urlencode(query)),
    )


def _url_owner(url: str) -> str:
    if not url:
        return ""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return (query.get(_OWNER_QUERY_KEY) or [""])[0]


def _claim_resource(
    state: dict,
    name: str,
    *,
    metadata: dict,
    current: dict,
    desired: dict,
    callback_fields: tuple[str, ...],
) -> None:
    """Record prior state and reject ownership conflicts before a mutation."""
    owner = state["owner"]
    resources = state["resources"]
    existing = resources.get(name)
    if existing:
        pending = existing.get("pending")
        if (
            current != existing["applied"]
            and current != existing["prior"]
            and current != desired
            and current != pending
        ):
            raise RuntimeError(
                f"{name} changed after this installation acquired it; "
                "refusing to overwrite another owner",
            )
        if pending is not None and current == pending:
            existing["applied"] = pending
        existing["metadata"] = metadata
        existing["pending"] = desired
        _save_state(state)
        return

    for field in callback_fields:
        value = str(current.get(field) or "")
        tagged_owner = _url_owner(value)
        if value and tagged_owner != owner:
            owner_label = tagged_owner or "an untagged external owner"
            raise RuntimeError(
                f"{name}.{field} is active for {owner_label}; refusing takeover",
            )

    resources[name] = {
        "metadata": metadata,
        "prior": current,
        "applied": current,
        "pending": desired,
    }
    _save_state(state)


def _commit_resource(state: dict, name: str, applied: dict) -> None:
    """Record provider state only after its mutation succeeds."""
    state["resources"][name]["applied"] = applied
    state["resources"][name].pop("pending", None)
    _save_state(state)


def _resource_matches(current: dict, applied: dict) -> bool:
    return all(
        (current.get(key) or "") == (value or "") for key, value in applied.items()
    )


def _resource_matches_owned_generation(current: dict, resource: dict) -> bool:
    if _resource_matches(current, resource["applied"]):
        return True
    pending = resource.get("pending")
    return pending is not None and _resource_matches(current, pending)


def _ownership() -> tuple[str, dict]:
    if not _ACTIVE_OWNER or not _ACTIVE_STATE:
        raise RuntimeError("webhook ownership is not initialized")
    return _ACTIVE_OWNER, _ACTIVE_STATE


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
    webhook = sender.get("webhook") or {}
    current = {
        "callback_method": webhook.get("callback_method") or "POST",
        "callback_url": webhook.get("callback_url") or "",
        "status_callback_method": webhook.get("status_callback_method") or "POST",
        "status_callback_url": webhook.get("status_callback_url") or "",
    }
    desired = {
        "callback_method": "POST",
        "callback_url": "",
        "status_callback_method": "POST",
        "status_callback_url": "",
    }
    sender_sid = sender["sid"]
    if check:
        if _whatsapp_is_poll_only(sender):
            print(f"[whatsapp] {number}: poll-only ✓")
            return True
        print(
            f"[whatsapp] {number}: DRIFT — inbound webhook set to "
            f"{webhook.get('callback_url')!r}",
        )
        return False
    _, state = _ownership()
    _claim_resource(
        state,
        "whatsapp_text",
        metadata={"kind": "whatsapp_text", "number": number, "sid": sender_sid},
        current=current,
        desired=desired,
        callback_fields=("callback_url", "status_callback_url"),
    )
    if current == desired:
        _commit_resource(state, "whatsapp_text", desired)
        print(f"[whatsapp] {number}: already poll-only ✓")
        return True
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
    _commit_resource(state, "whatsapp_text", desired)
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


def _env_enabled(env: dict, name: str, *, default: bool = False) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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

    Source installs retain best-effort WhatsApp calling. Compose's full internal
    calls profile sets ``SELF_HOST_REQUIRE_WHATSAPP_CALLS=true`` so missing
    credentials, Sender state, or calling capability fail startup.
    """
    required = _env_enabled(env, "SELF_HOST_REQUIRE_WHATSAPP_CALLS")
    account_sid = env.get("TWILIO_WA_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_WA_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        if required:
            print(
                "[whatsapp-call] ERROR — WhatsApp Twilio credentials are required",
                file=sys.stderr,
            )
            return False
        return True
    headers = _auth_header(account_sid, auth_token)
    sender = _find_whatsapp_sender(number, headers)
    if sender is None:
        if required:
            print(
                f"[whatsapp-call] ERROR — no Sender found for {number}",
                file=sys.stderr,
            )
            return False
        return True
    sender_sid = sender["sid"]
    try:
        full = _get_whatsapp_sender(sender_sid, headers)
    except urllib.error.HTTPError:
        full = sender
    cur_app = _sender_voice_app(full)

    if mode == "set-voice":
        owner, state = _ownership()
        want_voice = _tag_url(_wa_voice_target(public_url), owner)
        if check:
            if cur_app:
                app = _request(
                    "GET",
                    (
                        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
                        f"/Applications/{cur_app}.json"
                    ),
                    headers,
                )
                if (app.get("voice_url") or "") == want_voice:
                    print(f"[whatsapp-call] {number}: owned voice app attached ✓")
                    return True
            print(
                f"[whatsapp-call] {number}: DRIFT — no owned voice app attached "
                f"(want {want_voice})",
            )
            return False
        friendly_name = f"{_WA_VOICE_APP_FRIENDLY_NAME} {owner[:12]}"
        try:
            app_sid = _ensure_application(
                account_sid,
                headers,
                friendly_name,
                want_voice,
            )
            if not app_sid:
                print(
                    f"[whatsapp-call] ERROR — Twilio returned no voice app SID for {number}",
                    file=sys.stderr,
                )
                return False if required else True
            desired = {"voice_application_sid": app_sid}
            current = {"voice_application_sid": cur_app}
            _claim_resource(
                state,
                "whatsapp_voice",
                metadata={
                    "kind": "whatsapp_voice",
                    "number": number,
                    "sid": sender_sid,
                },
                current=current,
                desired=desired,
                callback_fields=(),
            )
            if app_sid and cur_app != app_sid:
                if cur_app:
                    current_app = _request(
                        "GET",
                        (
                            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
                            f"/Applications/{cur_app}.json"
                        ),
                        headers,
                    )
                    current_owner = _url_owner(current_app.get("voice_url") or "")
                    if current_owner != owner:
                        owner_label = current_owner or "an untagged external owner"
                        raise RuntimeError(
                            "WhatsApp voice is active for "
                            f"{owner_label}; refusing takeover",
                        )
                _set_sender_voice_app(sender_sid, headers, app_sid)
            _commit_resource(state, "whatsapp_voice", desired)
        except urllib.error.HTTPError as exc:
            print(
                f"[whatsapp-call] {number}: "
                f"{'ERROR' if required else 'WARN'} — could not attach voice app "
                f"({exc.code}); is WhatsApp Business Calling enabled?",
            )
            return not required
        print(f"[whatsapp-call] {number}: voice app {app_sid} → {want_voice} ✓")
        return True

    # Source-stack compatibility: explicit poll-only mode detaches only an app
    # owned by this installation. Shared or untagged applications are preserved.
    if not cur_app:
        print(f"[whatsapp-call] {number}: no voice app (poll-only) ✓")
        return True
    owner, state = _ownership()
    app = _request(
        "GET",
        (
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
            f"/Applications/{cur_app}.json"
        ),
        headers,
    )
    current_owner = _url_owner(app.get("voice_url") or "")
    if current_owner != owner:
        if check:
            print(
                f"[whatsapp-call] {number}: active external voice app preserved",
            )
            return True
        raise RuntimeError(
            f"WhatsApp voice app is owned by {current_owner or 'an external owner'}",
        )
    if check:
        print(f"[whatsapp-call] {number}: DRIFT — owned voice app still attached")
        return False
    desired = {"voice_application_sid": ""}
    current = {"voice_application_sid": cur_app}
    _claim_resource(
        state,
        "whatsapp_voice",
        metadata={"kind": "whatsapp_voice", "number": number, "sid": sender_sid},
        current=current,
        desired=desired,
        callback_fields=(),
    )
    try:
        _set_sender_voice_app(sender_sid, headers, "")
    except urllib.error.HTTPError as exc:
        print(
            f"[whatsapp-call] {number}: WARN — could not detach voice app ({exc.code})",
        )
        return False
    _commit_resource(state, "whatsapp_voice", desired)
    print(f"[whatsapp-call] {number}: detached owned voice app → poll-only ✓")
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
    return (
        not record.get("sms_url")
        and not record.get("voice_url")
        and not record.get("status_callback")
    )


def _public_url(env: dict) -> str:
    url = (
        env.get("UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL")
        or env.get("LOCAL_COMMS_PUBLIC_URL")
        or ""
    ).strip()
    return url.rstrip("/")


def _calls_enabled(env: dict) -> bool:
    return _env_enabled(env, "SELF_HOST_CALLS_ENABLED", default=True)


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
    required = _env_enabled(env, "SELF_HOST_REQUIRE_PHONE_CALLS")
    account_sid = env.get("TWILIO_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        print("[sms/voice] SKIP — TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set")
        return not required
    headers = _auth_header(account_sid, auth_token)
    record = _find_incoming_number(number, account_sid, headers)
    if record is None:
        print(
            f"[sms/voice] {number}: no IncomingPhoneNumber found (nothing to reconcile)",
        )
        return not required

    owner, state = _ownership()
    text_current = {"sms_url": record.get("sms_url") or ""}
    text_desired = {"sms_url": ""}

    if mode == "text-only":
        if check:
            if text_current == text_desired:
                print(f"[sms] {number}: poll-only ✓")
                return True
            print(
                f"[sms] {number}: DRIFT — sms_url={text_current['sms_url']!r} "
                "(want '')",
            )
            return False
        _claim_resource(
            state,
            "phone_text",
            metadata={
                "kind": "phone_text",
                "number": number,
                "sid": record["sid"],
                "account_sid": account_sid,
            },
            current=text_current,
            desired=text_desired,
            callback_fields=("sms_url",),
        )
        if text_current == text_desired:
            _commit_resource(state, "phone_text", text_desired)
            print(f"[sms] {number}: already poll-only ✓")
            return True
        _update_incoming_number(
            account_sid,
            record["sid"],
            headers,
            {"SmsUrl": ""},
        )
        _commit_resource(state, "phone_text", text_desired)
        print(f"[sms] {number}: cleared messaging webhook → poll-only ✓")
        return True

    if mode in {"set-voice", "voice-only"}:
        manage_text = mode == "set-voice"
        want_voice = _tag_url(_phone_voice_target(public_url), owner)
        want_status = _tag_url(_phone_status_target(public_url), owner)
        cur_voice = record.get("voice_url") or ""
        cur_sms = record.get("sms_url") or ""
        cur_status = record.get("status_callback") or ""
        if check:
            if (
                cur_voice == want_voice
                and cur_status == want_status
                and (not manage_text or not cur_sms)
            ):
                print(
                    f"[sms/voice] {number}: owned voice active (text poll-only) ✓",
                )
                return True
            print(
                f"[sms/voice] {number}: DRIFT — voice_url={cur_voice!r} "
                f"(want {want_voice!r}), status_callback={cur_status!r} "
                f"(want {want_status!r}), sms_url={cur_sms!r} (want '')",
            )
            return False
        voice_current = {
            "voice_method": record.get("voice_method") or "POST",
            "voice_url": cur_voice,
            "status_callback": cur_status,
            "status_callback_method": record.get("status_callback_method") or "POST",
        }
        voice_desired = {
            "voice_method": "POST",
            "voice_url": want_voice,
            "status_callback": want_status,
            "status_callback_method": "POST",
        }
        _claim_resource(
            state,
            "phone_voice",
            metadata={
                "kind": "phone_voice",
                "number": number,
                "sid": record["sid"],
                "account_sid": account_sid,
            },
            current=voice_current,
            desired=voice_desired,
            callback_fields=("voice_url", "status_callback"),
        )
        if manage_text:
            _claim_resource(
                state,
                "phone_text",
                metadata={
                    "kind": "phone_text",
                    "number": number,
                    "sid": record["sid"],
                    "account_sid": account_sid,
                },
                current=text_current,
                desired=text_desired,
                callback_fields=("sms_url",),
            )
        if voice_current == voice_desired and (
            not manage_text or text_current == text_desired
        ):
            _commit_resource(state, "phone_voice", voice_desired)
            if manage_text:
                _commit_resource(state, "phone_text", text_desired)
            print(
                f"[sms/voice] {number}: voice already → {want_voice} (text poll-only) ✓",
            )
            return True
        fields = {
            "VoiceUrl": want_voice,
            "VoiceMethod": "POST",
            "StatusCallback": want_status,
            "StatusCallbackMethod": "POST",
        }
        if manage_text:
            fields["SmsUrl"] = ""
        _update_incoming_number(
            account_sid,
            record["sid"],
            headers,
            fields,
        )
        _commit_resource(state, "phone_voice", voice_desired)
        if manage_text:
            _commit_resource(state, "phone_text", text_desired)
        suffix = ", text poll-only" if manage_text else ""
        print(f"[sms/voice] {number}: voice → {want_voice}{suffix} ✓")
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
    voice_current = {
        "voice_method": record.get("voice_method") or "POST",
        "voice_url": record.get("voice_url") or "",
        "status_callback": record.get("status_callback") or "",
        "status_callback_method": record.get("status_callback_method") or "POST",
    }
    voice_desired = {
        "voice_method": "POST",
        "voice_url": "",
        "status_callback": "",
        "status_callback_method": "POST",
    }
    _claim_resource(
        state,
        "phone_voice",
        metadata={
            "kind": "phone_voice",
            "number": number,
            "sid": record["sid"],
            "account_sid": account_sid,
        },
        current=voice_current,
        desired=voice_desired,
        callback_fields=("voice_url", "status_callback"),
    )
    _claim_resource(
        state,
        "phone_text",
        metadata={
            "kind": "phone_text",
            "number": number,
            "sid": record["sid"],
            "account_sid": account_sid,
        },
        current=text_current,
        desired=text_desired,
        callback_fields=("sms_url",),
    )
    _update_incoming_number(
        account_sid,
        record["sid"],
        headers,
        {"SmsUrl": "", "VoiceUrl": "", "StatusCallback": ""},
    )
    _commit_resource(state, "phone_voice", voice_desired)
    _commit_resource(state, "phone_text", text_desired)
    print(f"[sms/voice] {number}: cleared sms_url + voice_url → poll-only ✓")
    return True


def _release_resource(name: str, resource: dict, env: dict) -> bool:
    metadata = resource["metadata"]
    prior = resource["prior"]
    kind = metadata["kind"]

    if kind.startswith("phone_"):
        account_sid = env.get("TWILIO_ACCOUNT_SID", "")
        auth_token = env.get("TWILIO_AUTH_TOKEN", "")
        if not (account_sid and auth_token):
            print(f"ERROR: cannot release {name}: main Twilio credentials missing")
            return False
        headers = _auth_header(account_sid, auth_token)
        record = _find_incoming_number(metadata["number"], account_sid, headers)
        if record is None:
            print(f"ERROR: cannot release {name}: phone number no longer exists")
            return False
        if kind == "phone_text":
            current = {"sms_url": record.get("sms_url") or ""}
            fields = {"SmsUrl": prior.get("sms_url") or ""}
        else:
            current = {
                "voice_method": record.get("voice_method") or "POST",
                "voice_url": record.get("voice_url") or "",
                "status_callback": record.get("status_callback") or "",
                "status_callback_method": record.get("status_callback_method")
                or "POST",
            }
            fields = {
                "VoiceUrl": prior.get("voice_url") or "",
                "VoiceMethod": prior.get("voice_method") or "POST",
                "StatusCallback": prior.get("status_callback") or "",
                "StatusCallbackMethod": prior.get("status_callback_method") or "POST",
            }
        if _resource_matches(current, prior):
            print(f"[release] {name} was not changed; prior state intact ✓")
            return True
        if not _resource_matches_owned_generation(current, resource):
            print(
                f"ERROR: refusing to release {name}: callbacks changed after acquisition",
            )
            return False
        _update_incoming_number(
            account_sid,
            record["sid"],
            headers,
            fields,
        )
        print(f"[release] restored {name} prior state ✓")
        return True

    account_sid = env.get("TWILIO_WA_ACCOUNT_SID", "")
    auth_token = env.get("TWILIO_WA_AUTH_TOKEN", "")
    if not (account_sid and auth_token):
        print(f"ERROR: cannot release {name}: WhatsApp Twilio credentials missing")
        return False
    headers = _auth_header(account_sid, auth_token)
    sender = _find_whatsapp_sender(metadata["number"], headers)
    if sender is None:
        print(f"ERROR: cannot release {name}: WhatsApp Sender no longer exists")
        return False

    if kind == "whatsapp_text":
        webhook = sender.get("webhook") or {}
        current = {
            "callback_method": webhook.get("callback_method") or "POST",
            "callback_url": webhook.get("callback_url") or "",
            "status_callback_method": webhook.get("status_callback_method") or "POST",
            "status_callback_url": webhook.get("status_callback_url") or "",
        }
        if _resource_matches(current, prior):
            print(f"[release] {name} was not changed; prior state intact ✓")
            return True
        if not _resource_matches_owned_generation(current, resource):
            print(
                f"ERROR: refusing to release {name}: callbacks changed after acquisition",
            )
            return False
        body = json.dumps(
            {
                "webhook": {
                    "callback_method": prior.get("callback_method") or "POST",
                    "callback_url": prior.get("callback_url") or "",
                    "status_callback_method": prior.get("status_callback_method")
                    or "POST",
                    "status_callback_url": prior.get("status_callback_url") or "",
                },
            },
        ).encode()
        _request(
            "POST",
            f"{_SENDER_BASE}/{sender['sid']}",
            {**headers, "Content-Type": "application/json"},
            data=body,
        )
        print(f"[release] restored {name} prior state ✓")
        return True

    full = _get_whatsapp_sender(sender["sid"], headers)
    current = {"voice_application_sid": _sender_voice_app(full)}
    if _resource_matches(current, prior):
        print(f"[release] {name} was not changed; prior state intact ✓")
        return True
    if not _resource_matches_owned_generation(current, resource):
        print(
            f"ERROR: refusing to release {name}: voice app changed after acquisition",
        )
        return False
    _set_sender_voice_app(
        sender["sid"],
        headers,
        prior.get("voice_application_sid") or "",
    )
    print(f"[release] restored {name} prior state ✓")
    return True


def release_owned_state(env: dict, *, scope: str = "all") -> bool:
    owner = _installation_owner()
    state = _load_state(owner)
    resources = state["resources"]
    selected = {
        name: resource
        for name, resource in resources.items()
        if scope == "all"
        or (scope == "text" and resource["metadata"]["kind"].endswith("_text"))
        or (scope == "voice" and resource["metadata"]["kind"].endswith("_voice"))
    }
    if not selected:
        print(f"[release] no {scope} webhook state owned by this installation")
        return True

    ok = True
    for name, resource in selected.items():
        try:
            released = _release_resource(name, resource, env)
        except (OSError, RuntimeError, urllib.error.HTTPError) as exc:
            print(f"ERROR: release failed for {name}: {exc}", file=sys.stderr)
            released = False
        if released:
            resources.pop(name, None)
            _save_state(state)
        else:
            ok = False
    return ok


def main() -> int:
    global _ACTIVE_OWNER, _ACTIVE_STATE

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
        "--set-voice-only",
        action="store_true",
        help="Acquire phone and WhatsApp voice callbacks without changing text.",
    )
    mode_group.add_argument(
        "--acquire-text",
        action="store_true",
        help="Acquire messaging callbacks for poll-only bridge delivery.",
    )
    mode_group.add_argument(
        "--revert-voice",
        action="store_true",
        help="Acquire poll-only voice state without overwriting another owner.",
    )
    mode_group.add_argument(
        "--release",
        action="store_true",
        help="Restore all prior state still owned by this installation.",
    )
    mode_group.add_argument(
        "--release-text",
        action="store_true",
        help="Restore owned messaging callback state.",
    )
    mode_group.add_argument(
        "--release-voice",
        action="store_true",
        help="Restore owned call callback state.",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    _load_twilio_env_file(env)
    _ACTIVE_OWNER = _installation_owner()
    lock_path = _lock_file()
    lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    try:
        _ACTIVE_STATE = _load_state(_ACTIVE_OWNER)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.release or args.release_text or args.release_voice:
        scope = "all" if args.release else ("text" if args.release_text else "voice")
        try:
            return 0 if release_owned_state(env, scope=scope) else 1
        except (OSError, RuntimeError, urllib.error.HTTPError) as exc:
            print(f"ERROR: webhook release failed: {exc}", file=sys.stderr)
            return 2

    if args.set_voice:
        mode = "set-voice"
    elif args.set_voice_only:
        mode = "voice-only"
    elif args.acquire_text:
        mode = "text-only"
    elif args.revert_voice:
        mode = "revert-voice"
    elif _calls_enabled(env):
        mode = "set-voice"
    else:
        mode = "clear"

    public_url = _public_url(env)
    if mode in {"set-voice", "voice-only"} and not public_url:
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
            if mode != "voice-only":
                ok &= reconcile_whatsapp(
                    whatsapp_number,
                    env,
                    check=args.check,
                )
            if mode != "text-only":
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
    except (OSError, RuntimeError, urllib.error.HTTPError) as exc:
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            detail = f"{exc.code} {exc.read().decode()[:300]}"
        else:
            detail = str(exc)
        print(
            f"ERROR: Twilio webhook reconciliation failed: {detail}",
            file=sys.stderr,
        )
        return 2

    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
