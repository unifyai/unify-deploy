#!/usr/bin/env python3
"""Ensure a LiveKit Cloud inbound SIP trunk for the self-host (localhost) numbers.

Phone/WhatsApp calls in self-host bridge Twilio -> LiveKit Cloud SIP: the local
CM ingress answers the Twilio webhook, puts the caller in a Twilio conference,
and dials a SIP leg to ``sip:<number>@<LIVEKIT_SIP_URI>``. LiveKit routes that
SIP leg into the call's room via a per-call dispatch rule that the runtime
creates (``ensure_phone_dispatch_rule``) -- but only if an *inbound SIP trunk*
lists the dialed number. This script ensures such a trunk exists for the
localhost numbers. Idempotent: numbers already covered by any trunk are skipped.

Requires the LiveKit Cloud credentials (URL/key/secret) in the environment, as
loaded by ``self_host_env.sh`` (``self_host_export_livekit_cloud`` reads
``~/.droid/livekit_cloud.env``). Run with the droid venv python so the
``livekit`` package is importable.

Usage:
  python3 provision_call_sip.py            # create the trunk if numbers missing
  python3 provision_call_sip.py --check     # report only; exit 1 if any missing
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from livekit.api import (
    CreateSIPInboundTrunkRequest,
    LiveKitAPI,
    SIPInboundTrunkInfo,
)
from livekit.protocol.sip import ListSIPInboundTrunkRequest

SELF_HOST_TRUNK_NAME = "Droid_SelfHost"


def _normalize(number: str) -> str:
    number = number.strip().replace("whatsapp:", "").strip()
    if not number:
        return ""
    return number if number.startswith("+") else f"+{number}"


def _target_numbers() -> list[str]:
    """Collect the localhost call numbers from the environment (deduped)."""
    candidates = [
        os.environ.get("COMMS_BRIDGE_SMS_NUMBER", ""),
        os.environ.get("DROID_COORDINATOR_PHONE", ""),
        os.environ.get("DROID_COORDINATOR_PHONE_US", ""),
        os.environ.get("COMMS_BRIDGE_WHATSAPP_NUMBER", ""),
        os.environ.get("DROID_COORDINATOR_WHATSAPP_NUMBER", ""),
    ]
    numbers: list[str] = []
    for raw in candidates:
        normalized = _normalize(raw)
        if normalized and normalized not in numbers:
            numbers.append(normalized)
    return numbers


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: {name} must be set (LiveKit Cloud credentials)", file=sys.stderr)
        raise SystemExit(2)
    return value


async def _run(check: bool) -> int:
    targets = _target_numbers()
    if not targets:
        print(
            "ERROR: no localhost call numbers in env. Source selfhost/self_host_env.sh "
            "first, or run via 'stack.sh'.",
            file=sys.stderr,
        )
        return 2

    livekit_api = LiveKitAPI(
        url=_required_env("LIVEKIT_URL"),
        api_key=_required_env("LIVEKIT_API_KEY"),
        api_secret=_required_env("LIVEKIT_API_SECRET"),
    )
    try:
        trunks = await livekit_api.sip.list_sip_inbound_trunk(
            ListSIPInboundTrunkRequest(),
        )
        covered: set[str] = set()
        for trunk in trunks.items:
            covered.update(trunk.numbers)

        missing = [n for n in targets if n not in covered]

        if not missing:
            print(f"[call-sip] inbound SIP trunk already covers {targets} ✓")
            return 0

        if check:
            print(
                f"[call-sip] MISSING inbound SIP trunk for {missing} "
                f"(covered: {sorted(covered)})",
            )
            return 1

        trunk = SIPInboundTrunkInfo(
            name=SELF_HOST_TRUNK_NAME,
            numbers=missing,
            krisp_enabled=True,
        )
        created = await livekit_api.sip.create_sip_inbound_trunk(
            CreateSIPInboundTrunkRequest(trunk=trunk),
        )
        print(
            f"[call-sip] created inbound SIP trunk {created.sip_trunk_id} "
            f"for {missing} ✓",
        )
        sip_uri = os.environ.get("LIVEKIT_SIP_URI", "").strip()
        if not sip_uri:
            print(
                "[call-sip] WARNING: LIVEKIT_SIP_URI is not set — Twilio SIP legs "
                "will fail. Set it to the project SIP domain "
                "(e.g. <project>.sip.livekit.cloud).",
                file=sys.stderr,
            )
        return 0
    finally:
        await livekit_api.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether all localhost numbers are covered; exit 1 if not.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(check=args.check))


if __name__ == "__main__":
    raise SystemExit(main())
