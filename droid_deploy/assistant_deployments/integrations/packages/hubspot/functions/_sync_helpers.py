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
        "contacts": ("_sync_contacts", "sync_hubspot_contacts"),
        "companies": ("_sync_companies", "sync_hubspot_companies"),
        "deals": ("_sync_deals", "sync_hubspot_deals"),
        "tickets": ("_sync_tickets", "sync_hubspot_tickets"),
        "line_items": ("_sync_line_items", "sync_hubspot_line_items"),
        "products": ("_sync_products", "sync_hubspot_products"),
        "quotes": ("_sync_quotes", "sync_hubspot_quotes"),
        "owners": ("_sync_owners", "sync_hubspot_owners"),
        "pipelines": ("_sync_pipelines", "sync_hubspot_pipelines"),
        "lists": ("_sync_lists", "sync_hubspot_lists"),
        "associations": ("_sync_associations", "sync_hubspot_associations"),
        "properties": ("_sync_properties", "sync_hubspot_properties"),
        "feedback": ("_sync_feedback", "sync_hubspot_feedback"),
        "goals": ("_sync_goals", "sync_hubspot_goals"),
        "custom_objects": ("_sync_custom_objects", "sync_hubspot_custom_objects"),
    }


def engagement_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "calls": ("_sync_engagement_calls", "sync_hubspot_calls"),
        "emails": ("_sync_engagement_emails", "sync_hubspot_emails"),
        "meetings": ("_sync_engagement_meetings", "sync_hubspot_meetings"),
        "notes": ("_sync_engagement_notes", "sync_hubspot_notes"),
        "tasks": ("_sync_engagement_tasks", "sync_hubspot_tasks"),
    }


def marketing_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "forms": ("_sync_marketing_forms", "sync_hubspot_marketing_forms"),
        "campaigns": ("_sync_marketing_campaigns", "sync_hubspot_campaigns"),
        "emails": ("_sync_marketing_emails", "sync_hubspot_marketing_emails"),
        "workflows": ("_sync_marketing_workflows", "sync_hubspot_marketing_workflows"),
        "ctas": ("_sync_marketing_ctas", "sync_hubspot_ctas"),
        "subscriptions": (
            "_sync_marketing_subscriptions",
            "sync_hubspot_subscriptions",
        ),
        "events": ("_sync_marketing_events", "sync_hubspot_marketing_events"),
    }


def sales_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "sequences": ("_sync_sales_sequences", "sync_hubspot_sequences"),
        "templates": ("_sync_sales_templates", "sync_hubspot_sales_templates"),
        "snippets": ("_sync_sales_snippets", "sync_hubspot_sales_snippets"),
        "documents": ("_sync_sales_documents", "sync_hubspot_sales_documents"),
        "meeting_links": ("_sync_sales_meeting_links", "sync_hubspot_meeting_links"),
    }


def service_sync_registry() -> dict[str, tuple[str, str]]:
    return {
        "conversations": ("_sync_service_conversations", "sync_hubspot_conversations"),
        "kb_articles": ("_sync_service_knowledge_base", "sync_hubspot_kb_articles"),
        "chatflows": ("_sync_service_chatflows", "sync_hubspot_chatflows"),
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
