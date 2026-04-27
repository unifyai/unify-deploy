"""Scope catalog mapping feature names to provider-specific OAuth scopes.

Mirrored in Orchestra's ``web/api/assistant/scopes.py`` — these are static
data structures and an HTTP round-trip to fetch them isn't warranted.

Lives in ``common/`` so both the comms and adapters services can import
it without a cross-service dependency.
"""

from __future__ import annotations

GOOGLE_SCOPE_BUNDLES: dict[str, list[str]] = {
    "email": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    "calendar": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
    "contacts": [
        "https://www.googleapis.com/auth/contacts.readonly",
    ],
    "tasks": [
        "https://www.googleapis.com/auth/tasks",
    ],
}

GOOGLE_BASE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
]

MICROSOFT_SCOPE_BUNDLES: dict[str, list[str]] = {
    "email": ["Mail.Read", "Mail.Send", "Mail.ReadWrite"],
    "calendar": ["Calendars.Read", "Calendars.ReadWrite"],
    "drive": ["Files.Read", "Files.ReadWrite"],
    "contacts": ["Contacts.Read"],
    "teams": [
        "Chat.Read",
        "Chat.ReadWrite",
        "ChatMessage.Read",
        "ChannelMessage.Send",
        "ChannelMessage.Read.All",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "Channel.Create",
        "TeamMember.Read.All",
        "OnlineMeetings.ReadWrite",
    ],
    "sharepoint": ["Sites.Read.All", "Sites.ReadWrite.All"],
    "tasks": ["Tasks.Read", "Tasks.ReadWrite"],
}

MICROSOFT_BASE_SCOPES = ["User.Read", "offline_access"]

_BUNDLES = {
    "google": GOOGLE_SCOPE_BUNDLES,
    "microsoft": MICROSOFT_SCOPE_BUNDLES,
}
_BASE = {
    "google": GOOGLE_BASE_SCOPES,
    "microsoft": MICROSOFT_BASE_SCOPES,
}


def build_scope_string(provider: str, features: list[str]) -> str:
    """Resolve feature names to the union of provider scopes + base scopes."""
    bundles = _BUNDLES[provider]
    scopes: list[str] = list(_BASE[provider])
    for feat in features:
        scopes.extend(bundles[feat])
    seen: set[str] = set()
    unique: list[str] = []
    for s in scopes:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    if provider == "microsoft":
        return " ".join(
            (
                f"https://graph.microsoft.com/{s}"
                if not s.startswith("http") and s != "offline_access"
                else s
            )
            for s in unique
        )
    return " ".join(unique)
