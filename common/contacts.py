"""Shared contact-envelope helpers.

Contact dicts flow through Pub/Sub to Unity's ``comms_manager``.  The
canonical shape is what ``adapters.helpers.get_default_contacts``
emits: ``contact_id=0`` = the assistant, ``contact_id=1`` = the boss
(the primary user).

For event types where **the only known counter-party is the boss**
(outbound-initiated voice sessions that don't route through a
phone-number or email match — e.g. a Teams meeting joined via PSTN
dial-in, where we never see the organizer's phone number) the
``contacts`` list must contain **the boss only**.  Including the
assistant (``contact_id=0``) would cause Unity's organizer-selector

    next((c for c in contacts if c.get("contact_id") != 1), None)

to pick the assistant as the "organizer", which is semantically wrong.
"""

from __future__ import annotations

from typing import Any


def build_boss_contact(assistant: Any) -> dict[str, Any]:
    """Return the single boss-contact envelope (``contact_id=1``).

    ``assistant`` is the Orchestra assistant record (either the shape
    returned by ``adapters.helpers.get_assistant`` or
    ``communication.helpers._lookup_assistant`` — both surface the
    same ``user_*`` fields).  Missing fields default to empty string
    so downstream serialisation never fails on ``None``.
    """
    return {
        "contact_id": 1,
        "first_name": assistant.get("user_first_name") or "",
        "surname": assistant.get("user_surname") or "",
        "email_address": assistant.get("user_email") or "",
        "phone_number": assistant.get("user_number") or "",
        "whatsapp_number": assistant.get("user_whatsapp_number") or "",
        "discord_id": assistant.get("user_discord_id") or "",
        "bio": "",
        "rolling_summary": "",
        "should_respond": True,
        "response_policy": "",
    }
