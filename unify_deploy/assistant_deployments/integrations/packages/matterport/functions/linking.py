"""Cross-integration linking for Matterport.

Two cross-joins:

* **Matterport models <-> RealPage units** — populated three ways:
  internal-label convention (preferred, auto), manual via chat, address
  heuristic (deferred until RealPage lands).  v1 stores
  ``unit_id`` as a free string in ``Matterport/Links/ModelUnit``;
  tighten to typed FK once RealPage's units table arrives.

* **Matterport view stats <-> HubSpot leads** — primary join key is
  ``email`` from a ``utm_email`` query parameter embedded in the
  Showcase URL.  Customer must instrument their HubSpot landing pages
  to append ``?utm_email={{ contact.email }}`` to Showcase links.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def link_matterport_model_to_unit(
    model_id: str,
    unit_id: str,
    source: str = "manual",
    confidence: float = 1.0,
    mock: bool = True,
) -> dict:
    """Record a model<->unit link in DataManager.

    ``source`` is one of ``"manual"``, ``"address_match"``,
    ``"internal_label"``.  ``confidence`` is 0.0-1.0; manual and
    internal_label are typically 1.0.
    """
    import datetime as _dt

    record = {
        "model_id": str(model_id),
        "unit_id": str(unit_id),
        "source": source,
        "confidence": confidence,
        "linked_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
    }

    if mock:
        return {"link": record, "_mock": True}

    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        dm.ingest(
            "Matterport/Links/ModelUnit",
            rows=[record],
            unique_keys={"model_id": "str", "unit_id": "str"},
            infer_untyped_fields=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": f"failed to write link: {e!r}"}
    return {"link": record}


@custom_function()
async def lookup_matterport_model_for_unit(
    unit_id: str,
    mock: bool = True,
) -> dict:
    """Read back the model linked to a RealPage unit_id."""
    if mock:
        return {
            "unit_id": str(unit_id),
            "links": [
                {
                    "model_id": "mdl-mock-1",
                    "unit_id": str(unit_id),
                    "source": "internal_label",
                    "confidence": 1.0,
                },
            ],
        }

    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    safe = str(unit_id).replace("'", "''")
    try:
        rows = (
            await dm.filter(
                "Matterport/Links/ModelUnit",
                filter=f"`unit_id` == '{safe}'",
                limit=10,
            )
            or []
        )
    except Exception:
        rows = []
    return {"unit_id": str(unit_id), "links": rows}


@custom_function()
async def correlate_matterport_views_to_hubspot_leads(
    model_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Join Matterport view events to HubSpot contacts on email.

    Reads from ``Matterport/ViewStats/Events`` and
    ``HubSpot/CRM/Dimensions/Contacts``.  Requires the customer to have
    instrumented their Showcase URLs with ``?utm_email={{ contact.email }}``;
    without it the join is a no-op.
    """
    if mock:
        return {
            "model_id": model_id,
            "matches": [
                {
                    "lead_email": "alex@example.com",
                    "model_id": "mdl-mock-1",
                    "view_count": 3,
                    "last_viewed_at": "2026-04-29T11:42:18Z",
                    "hubspot_contact_id": "hs-contact-1",
                    "lifecyclestage": "marketingqualifiedlead",
                },
            ],
        }

    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    event_filter_clauses: list[str] = []
    if model_id:
        safe = str(model_id).replace("'", "''")
        event_filter_clauses.append(f"`model_id` == '{safe}'")
    if since:
        safe = since.replace("'", "''")
        event_filter_clauses.append(f"`occurred_at` >= '{safe}'")
    event_filter = " AND ".join(event_filter_clauses) or None

    try:
        events = (
            await dm.filter(
                "Matterport/ViewStats/Events",
                filter=event_filter,
                limit=2000,
            )
            or []
        )
    except Exception:
        events = []

    # Group events by referrer_email
    by_email: dict[str, dict] = {}
    for e in events:
        email = (e.get("referrer_email") or "").strip().lower()
        if not email:
            continue
        slot = by_email.setdefault(
            email,
            {
                "lead_email": email,
                "model_ids": set(),
                "view_count": 0,
                "last_viewed_at": None,
            },
        )
        slot["view_count"] += 1
        slot["model_ids"].add(e.get("model_id"))
        ts = e.get("occurred_at")
        if ts and (slot["last_viewed_at"] is None or ts > slot["last_viewed_at"]):
            slot["last_viewed_at"] = ts

    if not by_email:
        return {
            "model_id": model_id,
            "matches": [],
            "_note": "no events with utm_email",
        }

    # Look up HubSpot contacts for these emails
    emails_quoted = ", ".join(f"'{e}'" for e in by_email.keys())
    try:
        contacts = (
            await dm.filter(
                "HubSpot/CRM/Dimensions/Contacts",
                filter=f"`email` IN ({emails_quoted})",
                limit=1000,
            )
            or []
        )
    except Exception:
        contacts = []

    contact_by_email = {(c.get("email") or "").strip().lower(): c for c in contacts}

    matches: list[dict] = []
    for email, slot in by_email.items():
        contact = contact_by_email.get(email)
        matches.append(
            {
                "lead_email": email,
                "model_ids": sorted(m for m in slot["model_ids"] if m),
                "view_count": slot["view_count"],
                "last_viewed_at": slot["last_viewed_at"],
                "hubspot_contact_id": contact.get("hubspot_id") if contact else None,
                "lifecyclestage": contact.get("lifecyclestage") if contact else None,
            },
        )
    matches.sort(key=lambda r: r["view_count"], reverse=True)
    return {"model_id": model_id, "matches": matches[:limit]}
