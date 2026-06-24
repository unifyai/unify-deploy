"""Generic SOQL escape hatch + sObject describe.

Standard objects (Accounts, Contacts, Leads, Opportunities, Cases) have
dedicated functions; everything else — custom objects, custom fields,
ad-hoc joins, aggregations — is reachable through these two functions
without code changes.

Read-only by design.  ``run_salesforce_soql`` rejects DML-shaped strings
(``INSERT``, ``UPDATE``, ``DELETE``, ``UPSERT``) and the SOQL grammar
itself does not include them, but we still gate explicitly so a
mistakenly-pasted SOSL or apex snippet is rejected before it hits the
wire.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def run_salesforce_soql(
    query: str,
    mock: bool = True,
) -> dict:
    """Run an arbitrary SOQL ``SELECT`` against the connected org.

    Returns ``{"records": [...], "totalSize": int, "done": bool,
    "pages": int}`` on success, or the standard error envelope on
    failure.  Pagination via ``nextRecordsUrl`` is handled
    transparently by the HTTP client.

    Use this for custom objects, custom fields, joins, or aggregations
    not covered by the dedicated ``list_*`` / ``get_*`` / ``sync_*``
    functions.  Records are returned with their original Salesforce
    keys (PascalCase), unlike the standard-object functions which
    snake-case.

    Examples
    --------
    SELECT Id, Name FROM Account WHERE Industry = 'Technology' LIMIT 10
    SELECT COUNT(Id) total FROM Opportunity WHERE IsClosed = false
    SELECT Id, Custom_Field__c FROM My_Custom_Object__c LIMIT 50
    """
    if mock:
        return {
            "records": [
                {"Id": "001000000000001", "Name": "Acme Corp"},
                {"Id": "001000000000002", "Name": "Beta Industries"},
            ],
            "totalSize": 2,
            "done": True,
            "pages": 1,
            "_mock": True,
        }

    # SOQL is read-only, but reject anything that looks like a write
    # keyword before we hit the wire — easier-to-read error than
    # whatever Salesforce returns.
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "UPSERT ", "MERGE ")
    upper = (query or "").upper()
    for kw in forbidden:
        if kw in upper:
            return {
                "error": (
                    f"run_salesforce_soql is read-only; "
                    f"refusing query containing {kw.strip()}"
                ),
                "status_code": None,
            }

    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_query,
    )

    return await salesforce_query(query)


@custom_function()
async def describe_salesforce_object(
    sobject: str,
    mock: bool = True,
) -> dict:
    """Describe an sObject — fields, types, picklist values, references.

    Wraps ``GET /sobjects/<SObject>/describe``.  Useful for discovering
    custom fields before constructing a SOQL query, or for surfacing
    field metadata when building a UI.  Returns the full Salesforce
    describe payload verbatim (no flattening) — the structure is
    documented at developer.salesforce.com.
    """
    if mock:
        return {
            "name": sobject,
            "label": sobject,
            "custom": sobject.endswith("__c"),
            "fields": [
                {
                    "name": "Id",
                    "label": "Record ID",
                    "type": "id",
                    "nillable": False,
                },
                {
                    "name": "Name",
                    "label": "Name",
                    "type": "string",
                    "nillable": True,
                    "length": 80,
                },
                {
                    "name": "SystemModstamp",
                    "label": "System Modstamp",
                    "type": "datetime",
                    "nillable": False,
                },
            ],
            "_mock": True,
        }

    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_get,
    )

    return await salesforce_get(f"sobjects/{sobject}/describe")
