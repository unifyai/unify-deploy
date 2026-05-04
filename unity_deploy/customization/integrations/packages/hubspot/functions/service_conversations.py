"""HubSpot Conversations Inbox - threads + messages + reply (tier-gated)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_conversation_threads(
    after: str | None = None,
    limit: int = 50,
    inbox_id: int | None = None,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    """Paginate through conversations inbox threads."""
    if mock:
        base = {
            "inboxId": 1, "status": "OPEN",
            "subject": "Question about lease renewal",
            "createdAt": "2026-04-25T11:00:00Z",
            "latestMessageReceivedAt": "2026-04-26T15:00:00Z",
            "latestMessageSentAt": "2026-04-26T16:00:00Z",
            "assignedTo": "60001",
        }
        return {
            "results": [{**base, "id": f"th-{4000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    if inbox_id is not None:
        params["inboxId"] = inbox_id
    if status:
        params["status"] = status
    body = await hubspot_get("/conversations/v3/conversations/threads", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_conversation_thread(thread_id: str, mock: bool = True) -> dict:
    """Fetch a conversations inbox thread by ID."""
    if mock:
        return {
            "id": str(thread_id),
            "inboxId": 1, "status": "OPEN",
            "subject": "Question about lease renewal",
            "createdAt": "2026-04-25T11:00:00Z",
            "latestMessageReceivedAt": "2026-04-26T15:00:00Z",
            "latestMessageSentAt": "2026-04-26T16:00:00Z",
            "assignedTo": "60001",
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/conversations/v3/conversations/threads/{thread_id}")


@custom_function()
async def list_conversation_messages(
    thread_id: str,
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """Paginate through messages on one inbox thread."""
    if mock:
        base = {
            "type": "MESSAGE",
            "text": "When does my lease renewal go out?  I'd like to extend a year.",
            "subject": "Question about lease renewal",
            "createdAt": "2026-04-26T15:00:00Z",
            "senders": [{"actorId": "V-12345", "deliveryIdentifier": {"type": "HS_EMAIL_ADDRESS",
                                                                       "value": "tenant@example.com"}}],
            "recipients": [{"actorId": "A-60001"}],
            "channelId": 1, "channelAccountId": 100,
        }
        return {
            "thread_id": str(thread_id),
            "results": [{**base, "id": f"msg-{5000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get(
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        params=params,
    )
    if "error" in body:
        return body
    return {"thread_id": str(thread_id),
            "results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def send_conversation_reply(
    thread_id: str,
    text: str,
    confirm: bool = False,
    rich_text: str | None = None,
    mock: bool = True,
) -> dict:
    """Send a reply on a conversations inbox thread.  HIGH-STAKES: visible
    to the customer.  Requires ``confirm=True``."""
    if not confirm:
        return {"error": "send_conversation_reply requires confirm=True.",
                "thread_id": str(thread_id)}
    if mock:
        return {"status": "sent", "thread_id": str(thread_id), "text": text[:200]}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body = {
        "type": "MESSAGE",
        "text": text,
        "richText": rich_text or text,
        "senderActorId": None,
    }
    return await hubspot_post(
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        body,
    )


@custom_function()
async def sync_conversations(
    schema_version: str = "hubspot.service.conversations.v1",
    mock: bool = True,
) -> dict:
    """Sync thread metadata into a tables envelope.  Per-thread message bodies
    are fetched on-demand by ``list_conversation_messages``."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_thread,
    )

    if mock:
        base = {
            "inboxId": 1, "status": "OPEN",
            "subject": "Question about lease renewal",
            "createdAt": "2026-04-25T11:00:00Z",
            "latestMessageReceivedAt": "2026-04-26T15:00:00Z",
            "latestMessageSentAt": "2026-04-26T16:00:00Z",
            "assignedTo": "60001",
        }
        rows = [normalize_thread({**base, "id": f"th-{4000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"conversation_threads": rows},
            "metadata": {"object_type": "conversation_threads", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/conversations/v3/conversations/threads", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"conversation_threads": rows},
                    "metadata": {"object_type": "conversation_threads", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_thread(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"conversation_threads": rows},
        "metadata": {"object_type": "conversation_threads", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
