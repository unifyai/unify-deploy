"""Focused tests for assistant update event publishing."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from adapters.main import app
from common.settings import SETTINGS


class _PublishFuture:
    def result(self, timeout=None):
        return "message-123"


class _Publisher:
    def __init__(self):
        self.published: dict | None = None

    def topic_path(self, project_id: str, topic_name: str) -> str:
        return f"projects/{project_id}/topics/{topic_name}"

    def publish(self, topic_path: str, data: bytes, thread: str):
        self.published = {
            "topic_path": topic_path,
            "data": data,
            "thread": thread,
        }
        return _PublishFuture()


def _assistant(
    space_ids=None,
    space_summaries=None,
    *,
    is_coordinator: bool = False,
    org_id=None,
    workspace_org_id=None,
) -> dict:
    return {
        "assistant_id": "assistant-123",
        "api_key": "test-api-key",
        "user_id": "user-123",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "test@example.com",
        "user_number": "+1234567890",
        "user_whatsapp_number": "+1234567890",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_timezone": "UTC",
        "is_coordinator": is_coordinator,
        "org_id": org_id,
        "workspace_org_id": workspace_org_id,
        "is_local": False,
        "space_ids": space_ids or [],
        "space_summaries": space_summaries or [],
    }


def _contacts_payload() -> tuple[dict, int]:
    return (
        {
            "logs": [
                {"entries": {"contact_id": 0}},
                {"entries": {"contact_id": 1}},
            ],
        },
        200,
    )


def _client() -> TestClient:
    return TestClient(app)


def _published_payload(publisher: _Publisher) -> dict:
    return json.loads(publisher.published["data"].decode("utf-8"))


def _post_assistant_update(*, data: dict, assistant_data: dict, publisher: _Publisher):
    with (
        patch.object(SETTINGS, "orchestra_admin_key", "test-key"),
        patch("adapters.main.get_pubsub_client", return_value=publisher),
        patch("adapters.helpers.get_assistant", return_value=assistant_data),
        patch("adapters.helpers.get_contacts", return_value=_contacts_payload()),
        patch("adapters.helpers._WEBHOOK_BG_POOL.submit") as mock_submit,
    ):
        response = _client().post(
            "/assistant/update",
            data=data,
            headers={"Authorization": "Bearer test-key"},
        )
    return response, mock_submit


def test_membership_update_publishes_space_ids_inside_event():
    """Membership updates use source-fetched memberships over caller overrides."""

    publisher = _Publisher()
    source_summaries = [
        {
            "space_id": 9,
            "name": "Source of Truth",
            "description": "Fetched assistant membership payload.",
        },
    ]
    summaries = [
        {
            "space_id": 3,
            "name": "Ops",
            "description": "Operations workspace for customer support.",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "space_ids": "[3, 4]",
            "space_summaries": json.dumps(summaries),
            "update_kind": "membership",
        },
        assistant_data=_assistant(space_ids=[9], space_summaries=source_summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["thread"] == "assistant_update"
    assert published["event"]["assistant_id"] == "assistant-123"
    assert published["event"]["space_ids"] == [9]
    assert published["event"]["space_summaries"] == source_summaries
    assert published["event"]["update_kind"] == "membership"


def test_general_update_invokes_ensure_job():
    """General assistant updates keep the existing wake behavior."""

    publisher = _Publisher()
    summaries = [
        {
            "space_id": 5,
            "name": "Support",
            "description": "Support workspace for customer issues.",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={"assistant_id": "assistant-123"},
        assistant_data=_assistant(space_ids=[5, 6], space_summaries=summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_called_once()
    published = _published_payload(publisher)
    assert published["event"]["space_ids"] == [5, 6]
    assert published["event"]["space_summaries"] == summaries
    assert published["event"]["update_kind"] == "general"


def test_general_update_publishes_personal_coordinator_shape():
    """Personal Coordinators should publish explicit coordinator and null-org fields."""

    publisher = _Publisher()
    response, _ = _post_assistant_update(
        data={"assistant_id": "assistant-123"},
        assistant_data=_assistant(is_coordinator=True, org_id=None),
        publisher=publisher,
    )

    assert response.status_code == 200
    published = _published_payload(publisher)
    assert published["event"]["is_coordinator"] is True
    assert published["event"]["org_id"] is None
    assert published["event"]["workspace_org_id"] is None


def test_general_update_publishes_workspace_org_id():
    """Assistant update events should include explicit workspace attribution."""

    publisher = _Publisher()
    response, _ = _post_assistant_update(
        data={"assistant_id": "assistant-123"},
        assistant_data=_assistant(
            is_coordinator=True,
            org_id=7,
            workspace_org_id=9,
        ),
        publisher=publisher,
    )

    assert response.status_code == 200
    published = _published_payload(publisher)
    assert published["event"]["org_id"] == 7
    assert published["event"]["workspace_org_id"] == 9


def test_invalid_update_kind_returns_400():
    """The update discriminator accepts only the runtime-supported values."""

    with patch.object(SETTINGS, "orchestra_admin_key", "test-key"):
        response = _client().post(
            "/assistant/update",
            data={"assistant_id": "assistant-123", "update_kind": "config"},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "update_kind must be 'general' or 'membership'"


def test_invalid_space_ids_are_ignored():
    """Malformed caller membership overrides are ignored, not validated."""

    publisher = _Publisher()
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "space_ids": '[1, "bad"]',
            "update_kind": "membership",
        },
        assistant_data=_assistant(space_ids=[17]),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["event"]["space_ids"] == [17]


def test_invalid_space_summaries_are_ignored():
    """Malformed caller summary overrides are ignored, not validated."""

    publisher = _Publisher()
    source_summaries = [
        {
            "space_id": 22,
            "name": "Ops",
            "description": "Source summary",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "space_summaries": '[{"space_id": "bad", "name": "Ops", "description": "Bad"}]',
            "update_kind": "membership",
        },
        assistant_data=_assistant(space_ids=[22], space_summaries=source_summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["event"]["space_summaries"] == source_summaries
