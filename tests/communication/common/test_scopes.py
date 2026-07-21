"""Pure-helper coverage for the Adapters/Comms Google scope catalog mirror."""

from __future__ import annotations

from common.scopes import build_scope_string


def test_google_chat_bundle_emits_workspace_events_readonly_scopes() -> None:
    scope_str = build_scope_string("google", ["email", "chat"])
    parts = scope_str.split()
    assert len(parts) == len(set(parts))
    for scope in (
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/chat.users.readstate.readonly",
        "https://www.googleapis.com/auth/chat.users.availability.readonly",
    ):
        assert scope in parts
