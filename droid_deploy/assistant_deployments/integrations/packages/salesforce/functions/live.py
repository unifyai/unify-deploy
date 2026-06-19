"""Salesforce live-read tool.

Exposes a single registered function — ``salesforce_request`` — that
the assistant can call to issue any GET/POST/PATCH/PUT/DELETE against
the Salesforce REST API.  Auth (OAuth refresh-token flow with
in-process access-token caching), retry, 429 backoff, and 4xx/403
envelope shaping are handled by the underlying ``_client.salesforce_request``
helper, so the assistant only needs to know the API surface.

For SOQL queries, prefer the typed ``run_salesforce_soql`` /
``describe_salesforce_object`` helpers in ``soql.py`` — they handle
``nextRecordsUrl`` cursor following and the ``WHERE`` clause shape
checks.  This generic tool is for ad-hoc REST endpoints (sObject CRUD,
composite, tooling, metadata, bulk, etc.) and one-off mutations.

Bulk pulls and snapshots go through ``run_salesforce_sync_tick`` —
this tool returns one page only and does not auto-paginate.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def salesforce_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the Salesforce REST API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``, or
        ``"DELETE"``.
    path : str
        URL path.  Two forms accepted:
          * relative under ``/services/data/<api_version>/`` such as
            ``"sobjects/Account/001..."`` — the API-version prefix is
            added automatically; or
          * absolute starting with ``/`` such as
            ``"/services/data/v60.0/query/01g..."`` — used verbatim,
            useful for paginating SOQL ``nextRecordsUrl``.
    params : dict, optional
        Query-string parameters.  Common: ``fields`` (comma-separated
        list), ``limit``.
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.
        For sObject create/update use the standard
        ``{"FieldName": value, ...}`` shape.

    Returns
    -------
    dict
        On success: the parsed Salesforce JSON response.  Common shapes:
        ``{"records": [...], "totalSize": int, "done": bool, "nextRecordsUrl": "..."}``
        for queries, ``{"id": "...", "success": true, "errors": []}`` for
        creates, the full sObject for GETs by id.  On failure:
        ``{"error", "status_code", "hint"}``.

    Notes
    -----
    * **For SOQL, prefer ``run_salesforce_soql``** — it follows
      ``nextRecordsUrl`` automatically and handles the response shape.
    * **First page only.**  Bulk pulls go through sync; this helper
      returns the first page and you re-issue with the absolute
      ``nextRecordsUrl`` from the response.
    * **403 means scope/permission missing.**  Surface the hint to
      the user; do not retry blindly.
    * **Writes are not gated in code.**  Surface what's about to
      change to the user before issuing a destructive verb.
    """
    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_request as _request,
    )

    return await _request(method, path, params=params, body=body)
