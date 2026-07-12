"""Shared helpers for engagement_*.py files.

Underscore-prefixed: skipped by FunctionManager, not subject to the
function-compliance AST checks.  These helpers are imported INSIDE
``@custom_function`` bodies, never at module level.
"""

from __future__ import annotations


def now_ms_str() -> str:
    """Current epoch milliseconds as a string (HubSpot's ``hs_timestamp`` format)."""
    import datetime as _dt

    return str(int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000))


def build_associations(refs: list[dict], type_ids: dict[str, int]) -> list[dict]:
    """Convert ``[{object_type, id}, ...]`` to a HubSpot v4 association payload.

    ``type_ids`` maps the trailing-singular object_type (``"contact"``,
    ``"company"``, ``"deal"``, ``"ticket"``) to the HubSpot-defined
    primary association type id for the source object.  Pass the right
    map for the source object type (calls, emails, meetings, notes, tasks
    each have a different set).
    """
    out: list[dict] = []
    for ref in refs:
        obj = ref.get("object_type", "").rstrip("s")  # "contacts" -> "contact"
        type_id = type_ids.get(obj)
        if type_id is None:
            continue
        out.append(
            {
                "to": {"id": str(ref["id"])},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": type_id,
                    },
                ],
            },
        )
    return out
