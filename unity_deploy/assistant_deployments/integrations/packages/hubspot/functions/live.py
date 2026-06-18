"""HubSpot live-read tool.

Exposes a single registered function — ``hubspot_request`` — that the
assistant can call to issue any GET/POST/PATCH/PUT/DELETE against the
HubSpot API.  Auth (Private App bearer token), retry, 429 backoff,
and 4xx/403 envelope shaping are handled by the underlying
``_client.hubspot_request`` helper, so the assistant only needs to
know the API surface.

Bulk pulls and snapshots go through ``run_hubspot_sync_tick`` — this
tool is for ad-hoc reads and one-off writes.  HubSpot CRM search
(``POST /crm/v3/objects/{type}/search`` with ``filterGroups`` /
``sorts`` / ``properties``) has non-trivial filter semantics; the
typed ``search_hubspot`` helper in ``search.py`` is the better entry
point for cross-object lookups.  Cross-object association reads/writes
are similarly idiom-heavy — prefer the typed helpers in
``associations.py``.  For property-schema discovery use the typed
helpers in ``properties.py``.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def hubspot_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the HubSpot API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``, or
        ``"DELETE"``.
    path : str
        URL path beginning with a slash, e.g.
        ``"/crm/v3/objects/contacts/12345"`` or
        ``"/marketing/v3/forms"``.  The HubSpot API base URL is added
        automatically.
    params : dict, optional
        Query-string parameters.  Common ones: ``properties`` (comma-
        separated list of property names to fetch), ``after`` (pagination
        cursor), ``limit`` (page size, max 100 for most endpoints), ``q``
        (full-text query on some endpoints).
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.
        For CRM search, prefer the typed ``search_hubspot`` helper.

    Returns
    -------
    dict
        On success: the parsed HubSpot JSON response (typically
        ``{"results": [...], "paging": {"next": {"after": ...}}}`` for
        list endpoints, or the single object for GETs by id).  On
        failure: ``{"error", "status_code", "hint"}`` — read ``hint``
        for guidance.

    Notes
    -----
    * **First page only.**  Bulk pulls go through sync; this helper
      does not auto-paginate.  Use ``params={"after": "<cursor>"}`` to
      re-issue for subsequent pages.
    * **Tier gating.**  403 means the Private App lacks the scope, or
      the customer's HubSpot tier doesn't include this surface — surface
      the hint to the user, do not retry blindly.
    * **Writes are not gated in code.**  The caller is responsible for
      surfacing what's about to change to the user before issuing a
      destructive verb.
    """
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_request as _request,
    )

    return await _request(method, path, params=params, body=body)
