"""Local DataManager queries against the Salesforce mirror.

Once ``run_salesforce_sync_tick`` has populated the ``Salesforce/*``
contexts, these functions answer questions without round-tripping to
Salesforce.  Preferred for analytics and any read where freshness
within the configured cadence is acceptable.

For ad-hoc fields not in the standard sync envelope, drop down to
``run_salesforce_soql`` (which always hits the live API).

Each function defers to ``_sync_helpers.query_local_table`` (imported
inside the body to satisfy FunctionManager isolation).
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def query_local_salesforce_accounts(
    limit: int = 50,
    industry: str | None = None,
    mock: bool = True,
) -> dict:
    """Query the locally-synced Accounts table."""
    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        query_local_table,
    )

    return await query_local_table(
        "Salesforce/Accounts",
        limit=limit,
        equality_filters={"industry": industry} if industry else None,
        order_by="system_modstamp desc",
        mock=mock,
        mock_rows=[
            {
                "id": "001000000000001",
                "name": "Acme Corp",
                "industry": "Technology",
                "annual_revenue": 5_000_000,
            }
        ],
    )


@custom_function()
async def query_local_salesforce_contacts(
    limit: int = 50,
    account_id: str | None = None,
    mock: bool = True,
) -> dict:
    """Query the locally-synced Contacts table."""
    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        query_local_table,
    )

    return await query_local_table(
        "Salesforce/Contacts",
        limit=limit,
        equality_filters={"account_id": account_id} if account_id else None,
        order_by="system_modstamp desc",
        mock=mock,
        mock_rows=[
            {
                "id": "003000000000001",
                "name": "Alex Example",
                "email": "alex@example.test",
                "account_id": "001000000000001",
            }
        ],
    )


@custom_function()
async def query_local_salesforce_leads(
    limit: int = 50,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    """Query the locally-synced Leads table."""
    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        query_local_table,
    )

    return await query_local_table(
        "Salesforce/Leads",
        limit=limit,
        equality_filters={"status": status} if status else None,
        order_by="system_modstamp desc",
        mock=mock,
        mock_rows=[
            {
                "id": "00Q000000000001",
                "name": "Pat Prospect",
                "email": "pat@prospect.test",
                "company": "Prospect Co",
                "status": "Working - Contacted",
            }
        ],
    )


@custom_function()
async def query_local_salesforce_opportunities(
    limit: int = 50,
    account_id: str | None = None,
    open_only: bool = False,
    mock: bool = True,
) -> dict:
    """Query the locally-synced Opportunities table."""
    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        query_local_table,
    )

    filters: dict = {}
    if account_id:
        filters["account_id"] = account_id
    if open_only:
        filters["is_closed"] = False
    return await query_local_table(
        "Salesforce/Opportunities",
        limit=limit,
        equality_filters=filters or None,
        order_by="system_modstamp desc",
        mock=mock,
        mock_rows=[
            {
                "id": "006000000000001",
                "name": "Acme — Annual Renewal",
                "account_id": "001000000000001",
                "amount": 120_000.0,
                "stage_name": "Proposal/Price Quote",
                "is_closed": False,
            }
        ],
    )


@custom_function()
async def query_local_salesforce_cases(
    limit: int = 50,
    account_id: str | None = None,
    open_only: bool = False,
    mock: bool = True,
) -> dict:
    """Query the locally-synced Cases table."""
    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        query_local_table,
    )

    filters: dict = {}
    if account_id:
        filters["account_id"] = account_id
    if open_only:
        filters["is_closed"] = False
    return await query_local_table(
        "Salesforce/Cases",
        limit=limit,
        equality_filters=filters or None,
        order_by="system_modstamp desc",
        mock=mock,
        mock_rows=[
            {
                "id": "500000000000001",
                "case_number": "00001234",
                "subject": "Login timeout on Acme tenant",
                "status": "Working",
                "priority": "High",
                "account_id": "001000000000001",
            }
        ],
    )
