"""Employment Hero live-read tool.

Exposes a single registered function — ``employmenthero_request`` —
that the assistant can call to issue any GET/POST/PATCH/PUT/DELETE
against the Employment Hero API.  Auth (OAuth refresh-token flow with
in-process access-token caching), retry, 429 backoff, and 4xx/403
envelope shaping are handled by the underlying ``_client.eh_request``
helper, so the assistant only needs to know the API surface.

Bulk pulls and snapshots go through ``run_employmenthero_sync_tick`` —
this tool is for ad-hoc reads and one-off writes.

There is **no code gate** on writes or sensitive reads.  The assistant
must apply the rules in ``employmenthero_high_stakes_writes.md`` and
``employmenthero_sensitive_data.md`` before issuing destructive verbs
or returning sensitive PII to the user.  Key rules summarised below
are also surfaced in the tool's docstring so they're always in context.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def employmenthero_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the Employment Hero API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``, or
        ``"DELETE"``.
    path : str
        URL path beginning with a slash, e.g. ``"/api/v1/me"``,
        ``"/api/v1/organisations"``, or
        ``"/api/v1/organisations/{org_id}/employees"``.  The Employment
        Hero base URL is added automatically.
    params : dict, optional
        Query-string parameters.  EH list endpoints typically accept
        ``page_index`` and ``page_size`` (default 25, max 200).
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.

    Returns
    -------
    dict
        On success: the parsed Employment Hero JSON response (typically
        ``{"data": {"items": [...], "page_index": ..., "total_pages":
        ...}}`` for list endpoints, or ``{"data": {...}}`` for single
        records).  On failure: ``{"error", "status_code", "hint"}``.

    Notes
    -----
    Active organisation: most paths take an ``{org_id}`` segment.  If
    you don't know it, call ``get_employmenthero_active_organisation``
    first — it resolves ``EMPLOYMENTHERO_ORGANISATION_ID`` (set by the
    Console Connect flow) or falls back to the first accessible org.

    First page only: this helper does not auto-paginate.

    **High-stakes writes — confirm with the user first.** EH backs
    real payroll, HRIS, and compliance records.  Before issuing a
    write (POST/PATCH/PUT/DELETE), surface what's about to change and
    get explicit user confirmation.  Particularly sensitive surfaces:

    * Banking details, super funds, tax declarations — never write
      without explicit confirmation; never read into actor-visible
      output without redaction.
    * Pay runs, pay categories, employment terms — financial impact;
      changes propagate to actual pay.
    * Employee personal data, medical disclosures, documents —
      sensitive PII, often jurisdiction-regulated (UK / AU / NZ / SG).
    * Leave balances, timesheets — entitlement and payroll impact.
    * Onboarding state, termination flows — employment status.

    **Sensitive reads — apply minimum-disclosure.**  Even reads can
    leak PII into chat history.  When the user's question can be
    answered with aggregates or counts, prefer those; when exact
    figures are needed (e.g. "what's the gross on this payslip"),
    confirm the user has authorisation to see them before fetching,
    and consider returning a redacted form.

    **Tier and scope gating.**  403 means the connected user's EH
    role/scope doesn't cover the endpoint.  Surface the hint to the
    user; do not retry blindly.
    """
    from unify_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_request,
    )

    return await eh_request(method, path, params=params, body=body)
