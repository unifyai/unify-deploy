"""Focused tests for assistant update event publishing."""

import os

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from adapters.main import app


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
    team_ids=None,
    team_summaries=None,
    *,
    is_coordinator: bool = False,
    org_id=None,
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
        "is_local": False,
        "team_ids": team_ids or [],
        "team_summaries": team_summaries or [],
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
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-key"}),
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


def test_membership_update_publishes_team_ids_inside_event():
    """Membership updates use source-fetched memberships over caller overrides."""

    publisher = _Publisher()
    source_summaries = [
        {
            "team_id": 9,
            "name": "Source of Truth",
            "description": "Fetched assistant membership payload.",
        },
    ]
    summaries = [
        {
            "team_id": 3,
            "name": "Ops",
            "description": "Operations workspace for customer support.",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "team_ids": "[3, 4]",
            "team_summaries": json.dumps(summaries),
            "update_kind": "membership",
        },
        assistant_data=_assistant(team_ids=[9], team_summaries=source_summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["thread"] == "assistant_update"
    assert published["event"]["assistant_id"] == "assistant-123"
    assert published["event"]["team_ids"] == [9]
    assert published["event"]["team_summaries"] == source_summaries
    assert published["event"]["update_kind"] == "membership"


def test_general_update_invokes_ensure_job():
    """General assistant updates keep the existing wake behavior."""

    publisher = _Publisher()
    summaries = [
        {
            "team_id": 5,
            "name": "Support",
            "description": "Support workspace for customer issues.",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={"assistant_id": "assistant-123"},
        assistant_data=_assistant(team_ids=[5, 6], team_summaries=summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_called_once()
    published = _published_payload(publisher)
    assert published["event"]["team_ids"] == [5, 6]
    assert published["event"]["team_summaries"] == summaries
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


def test_general_update_publishes_no_null_runtime_strings():
    """Runtime string fields are env-safe even when source metadata is sparse."""

    publisher = _Publisher()
    sparse_assistant = {
        **_assistant(is_coordinator=True),
        "user_first_name": None,
        "user_surname": None,
        "user_number": None,
        "user_whatsapp_number": "+4915550100009",
        "assistant_surname": None,
        "assistant_age": None,
        "assistant_nationality": None,
        "assistant_about": None,
        "assistant_job_title": None,
        "assistant_timezone": None,
        "assistant_number": None,
        "assistant_whatsapp_number": None,
        "assistant_discord_bot_id": None,
        "assistant_slack_bot_user_id": None,
        "voice_provider": None,
        "voice_id": None,
        "desktop_mode": None,
    }
    response, _ = _post_assistant_update(
        data={"assistant_id": "assistant-123"},
        assistant_data=sparse_assistant,
        publisher=publisher,
    )

    assert response.status_code == 200
    event = _published_payload(publisher)["event"]
    assert event["user_whatsapp_number"] == "+4915550100009"
    for field in [
        "user_first_name",
        "user_surname",
        "user_number",
        "assistant_surname",
        "assistant_age",
        "assistant_nationality",
        "assistant_about",
        "assistant_job_title",
        "assistant_timezone",
        "assistant_number",
        "assistant_whatsapp_number",
        "assistant_discord_bot_id",
        "assistant_slack_bot_user_id",
        "voice_provider",
        "voice_id",
        "desktop_mode",
    ]:
        assert event[field] == ""


def test_invalid_update_kind_returns_400():
    """The update discriminator accepts only the runtime-supported values."""

    with patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-key"}):
        response = _client().post(
            "/assistant/update",
            data={"assistant_id": "assistant-123", "update_kind": "config"},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "update_kind must be 'general' or 'membership'"


def test_invalid_team_ids_are_ignored():
    """Malformed caller membership overrides are ignored, not validated."""

    publisher = _Publisher()
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "team_ids": '[1, "bad"]',
            "update_kind": "membership",
        },
        assistant_data=_assistant(team_ids=[17]),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["event"]["team_ids"] == [17]


def test_invalid_team_summaries_are_ignored():
    """Malformed caller summary overrides are ignored, not validated."""

    publisher = _Publisher()
    source_summaries = [
        {
            "team_id": 22,
            "name": "Ops",
            "description": "Source summary",
        },
    ]
    response, mock_submit = _post_assistant_update(
        data={
            "assistant_id": "assistant-123",
            "team_summaries": '[{"team_id": "bad", "name": "Ops", "description": "Bad"}]',
            "update_kind": "membership",
        },
        assistant_data=_assistant(team_ids=[22], team_summaries=source_summaries),
        publisher=publisher,
    )

    assert response.status_code == 200
    mock_submit.assert_not_called()
    published = _published_payload(publisher)
    assert published["event"]["team_summaries"] == source_summaries
