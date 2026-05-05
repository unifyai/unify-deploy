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
        "contacts": ("contacts", "sync_hubspot_contacts"),
        "companies": ("companies", "sync_hubspot_companies"),
        "deals": ("deals", "sync_hubspot_deals"),
        "tickets": ("tickets", "sync_hubspot_tickets"),
        "line_items": ("line_items", "sync_hubspot_line_items"),
        "products": ("products", "sync_hubspot_products"),
        "quotes": ("quotes", "sync_hubspot_quotes"),
        "owners": ("owners", "sync_hubspot_owners"),
        "pipelines": ("pipelines", "sync_hubspot_pipelines"),
        "lists": ("lists", "sync_hubspot_lists"),
        "associations": ("associations", "sync_hubspot_associations"),
        "properties": ("properties", "sync_hubspot_properties"),
        "feedback": ("feedback", "sync_hubspot_feedback"),
        "goals": ("goals", "sync_hubspot_goals"),
        "custom_objects": ("custom_objects", "sync_hubspot_custom_objects"),
    }


def engagement_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "calls": ("engagement_calls", "sync_hubspot_calls"),
        "emails": ("engagement_emails", "sync_hubspot_emails"),
        "meetings": ("engagement_meetings", "sync_hubspot_meetings"),
        "notes": ("engagement_notes", "sync_hubspot_notes"),
        "tasks": ("engagement_tasks", "sync_hubspot_tasks"),
    }


def marketing_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "forms": ("marketing_forms", "sync_hubspot_marketing_forms"),
        "campaigns": ("marketing_campaigns", "sync_hubspot_campaigns"),
        "emails": ("marketing_emails", "sync_hubspot_marketing_emails"),
        "workflows": ("marketing_workflows", "sync_hubspot_marketing_workflows"),
        "ctas": ("marketing_ctas", "sync_hubspot_ctas"),
        "subscriptions": ("marketing_subscriptions", "sync_hubspot_subscriptions"),
        "events": ("marketing_events", "sync_hubspot_marketing_events"),
    }


def sales_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "sequences": ("sales_sequences", "sync_hubspot_sequences"),
        "templates": ("sales_templates", "sync_hubspot_sales_templates"),
        "snippets": ("sales_snippets", "sync_hubspot_sales_snippets"),
        "documents": ("sales_documents", "sync_hubspot_sales_documents"),
        "meeting_links": ("sales_meeting_links", "sync_hubspot_meeting_links"),
    }


def service_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "conversations": ("service_conversations", "sync_hubspot_conversations"),
        "kb_articles": ("service_knowledge_base", "sync_hubspot_kb_articles"),
        "chatflows": ("service_chatflows", "sync_hubspot_chatflows"),
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
        "contacts": "HubSpot/CRM/Dimensions/Contacts",
        "companies": "HubSpot/CRM/Dimensions/Companies",
        "deals": "HubSpot/CRM/Dimensions/Deals",
        "tickets": "HubSpot/CRM/Dimensions/Tickets",
        "custom_objects": "HubSpot/CRM/CustomObjects",
        "sync_state": "HubSpot/CRM/Meta/SyncState",
    }


def engagement_contexts() -> dict[str, str]:
    """Per-engagement-type DataManager context."""
    return {
        "call": "HubSpot/CRM/Engagements/Calls",
        "email": "HubSpot/CRM/Engagements/Emails",
        "meeting": "HubSpot/CRM/Engagements/Meetings",
        "note": "HubSpot/CRM/Engagements/Notes",
        "task": "HubSpot/CRM/Engagements/Tasks",
    }


def engagement_body_columns() -> dict[str, str]:
    """Per-engagement-type body column for semantic search."""
    return {
        "call": "hs_call_body",
        "email": "hs_email_text",
        "meeting": "hs_meeting_body",
        "note": "hs_note_body",
        "task": "hs_task_body",
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
