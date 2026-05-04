"""Shared helpers for the sync orchestrator and local-query functions.

Underscore-prefixed: skipped by FunctionManager and the function-compliance
AST checks.  Holds the per-object-type sync function registry,
DataManager freshness helpers, and the SyncState loader.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Sync function registries - which ``sync_*`` to dispatch for each
# object key.  The orchestrator imports these inside its body.
# ---------------------------------------------------------------------------


def crm_sync_registry() -> dict[str, tuple[str, str]]:
    """Object key -> (function module stem, function name) for CRM objects."""
    return {
        "contacts":          ("contacts",         "sync_contacts"),
        "companies":         ("companies",        "sync_companies"),
        "deals":             ("deals",            "sync_deals"),
        "tickets":           ("tickets",          "sync_tickets"),
        "line_items":        ("line_items",       "sync_line_items"),
        "products":          ("products",         "sync_products"),
        "quotes":            ("quotes",           "sync_quotes"),
        "owners":            ("owners",           "sync_owners"),
        "pipelines":         ("pipelines",        "sync_pipelines"),
        "lists":             ("lists",            "sync_lists"),
        "associations":      ("associations",     "sync_associations"),
        "properties":        ("properties",       "sync_properties"),
        "feedback":          ("feedback",         "sync_feedback"),
        "goals":             ("goals",            "sync_goals"),
        "custom_objects":    ("custom_objects",   "sync_custom_objects"),
    }


def engagement_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "calls":    ("engagement_calls",    "sync_calls"),
        "emails":   ("engagement_emails",   "sync_emails"),
        "meetings": ("engagement_meetings", "sync_meetings"),
        "notes":    ("engagement_notes",    "sync_notes"),
        "tasks":    ("engagement_tasks",    "sync_tasks"),
    }


def marketing_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "forms":         ("marketing_forms",         "sync_marketing_forms"),
        "campaigns":     ("marketing_campaigns",     "sync_campaigns"),
        "emails":        ("marketing_emails",        "sync_marketing_emails"),
        "workflows":     ("marketing_workflows",     "sync_marketing_workflows"),
        "ctas":          ("marketing_ctas",          "sync_ctas"),
        "subscriptions": ("marketing_subscriptions", "sync_subscriptions"),
        "events":        ("marketing_events",        "sync_marketing_events"),
    }


def sales_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "sequences":     ("sales_sequences",     "sync_sequences"),
        "templates":     ("sales_templates",     "sync_sales_templates"),
        "snippets":      ("sales_snippets",      "sync_sales_snippets"),
        "documents":     ("sales_documents",     "sync_sales_documents"),
        "meeting_links": ("sales_meeting_links", "sync_meeting_links"),
    }


def service_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "conversations": ("service_conversations",  "sync_conversations"),
        "kb_articles":   ("service_knowledge_base", "sync_kb_articles"),
        "chatflows":     ("service_chatflows",      "sync_chatflows"),
    }


# ---------------------------------------------------------------------------
# DataManager freshness + state helpers
# ---------------------------------------------------------------------------


def seconds_since(iso: str | None) -> float | None:
    """Seconds elapsed since an ISO-8601 timestamp; None if input is empty/invalid."""
    if not iso:
        return None
    import datetime as _dt

    try:
        ts = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (_dt.datetime.now(tz=_dt.timezone.utc) - ts).total_seconds()


def local_query_contexts() -> dict[str, str]:
    """Canonical DataManager context paths used by ``local_query.py``."""
    return {
        "contacts":   "HubSpot/CRM/Dimensions/Contacts",
        "companies":  "HubSpot/CRM/Dimensions/Companies",
        "deals":      "HubSpot/CRM/Dimensions/Deals",
        "tickets":    "HubSpot/CRM/Dimensions/Tickets",
        "custom_objects": "HubSpot/CRM/CustomObjects",
        "sync_state": "HubSpot/CRM/Meta/SyncState",
    }


def engagement_contexts() -> dict[str, str]:
    """Per-engagement-type DataManager context."""
    return {
        "call":    "HubSpot/CRM/Engagements/Calls",
        "email":   "HubSpot/CRM/Engagements/Emails",
        "meeting": "HubSpot/CRM/Engagements/Meetings",
        "note":    "HubSpot/CRM/Engagements/Notes",
        "task":    "HubSpot/CRM/Engagements/Tasks",
    }


def engagement_body_columns() -> dict[str, str]:
    """Per-engagement-type body column for semantic search."""
    return {
        "call":    "hs_call_body",
        "email":   "hs_email_text",
        "meeting": "hs_meeting_body",
        "note":    "hs_note_body",
        "task":    "hs_task_body",
    }


def build_filter(pairs: list[tuple[str, str, str | None]]) -> str:
    """Build a DataManager filter expression from non-None ``(col, op, value)`` pairs."""
    parts = []
    for col, op, value in pairs:
        if value is None or value == "":
            continue
        parts.append(f"`{col}` {op} {value!r}")
    return " and ".join(parts)


def mock_freshness_envelope() -> dict:
    """Deterministic freshness envelope used by ``query_local_*(mock=True)``."""
    return {
        "last_synced_at": "2026-04-26T15:00:00Z",
        "is_fresh": True,
        "threshold_seconds": 3600,
    }
