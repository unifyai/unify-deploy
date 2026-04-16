"""Scope catalog mapping feature names to provider-specific OAuth scopes.

Duplicated from Orchestra's ``web/api/assistant/scopes.py`` — these are
static data structures and an HTTP round-trip to fetch them isn't warranted.
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
        "https://www.googleapis.com/auth/drive.file",
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
    "teams": ["Chat.Read", "Chat.ReadWrite", "ChannelMessage.Send"],
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


def available_features(provider: str) -> list[str]:
    """Return the feature names available for *provider*."""
    return list(_BUNDLES[provider])


def build_scope_string(provider: str, features: list[str]) -> str:
    """Resolve feature names to the union of provider scopes + base scopes."""
    bundles = _BUNDLES[provider]
    scopes: list[str] = list(_BASE[provider])
    for feat in features:
        scopes.extend(bundles[feat])
    # Deduplicate while preserving order
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


def map_scopes_to_features(provider: str, granted_scopes: str) -> list[str]:
    """Map a raw scope string back to feature names whose bundles are fully covered."""
    bundles = _BUNDLES[provider]
    granted = set(granted_scopes.split())
    features: list[str] = []
    for feat, required in bundles.items():
        if provider == "microsoft":
            required_full = {
                (
                    f"https://graph.microsoft.com/{s}"
                    if not s.startswith("http") and s != "offline_access"
                    else s
                )
                for s in required
            }
        else:
            required_full = set(required)
        if required_full.issubset(granted):
            features.append(feat)
    return features


def has_feature(provider: str, granted_scopes: str, feature: str) -> bool:
    """Return True if *feature*'s scopes are fully covered by *granted_scopes*."""
    return feature in map_scopes_to_features(provider, granted_scopes)
