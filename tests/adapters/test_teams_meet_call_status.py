"""Unit tests for the dedicated ``/twilio/teams-meet-call-status`` endpoint.

The endpoint:

* Routes contact + assistant lookup through ``build_webhook_context``
  so Unity's container is kept warm for the duration of the meeting
  (``ensure_job=True``).
* Emits ``teams_meet_started`` on ``in-progress`` and
  ``teams_meet_ended`` on any terminal status.  Intermediate statuses
  (``queued`` / ``ringing`` / ``initiated``) are dropped.
* Publishes **boss-only** contacts (never the assistant), so Unity's
  organizer-or-boss selector lands on the boss when the organizer
  isn't resolvable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import adapters.main as adapters_main
from adapters.main import app, validate_twilio_signature


FAKE_ASSISTANT = {
    "assistant_id": "42",
    "user_id": "7",
    "api_key": "sk-test",
    "assistant_first_name": "Astra",
    "assistant_surname": "Bot",
    "assistant_email": "astra@contoso.com",
    "assistant_number": "+15550100006",
    "user_first_name": "Julia",
    "user_surname": "Boss",
    "user_email": "julia@contoso.com",
    "user_number": "+14155550001",
    "user_whatsapp_number": "",
    "user_discord_id": "",
}


@pytest.fixture
def client():
    # Skip Twilio signature validation in unit tests — the signing path
    # is exercised by ``test_twilio_validation.py``.
    app.dependency_overrides[validate_twilio_signature] = lambda: None
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(validate_twilio_signature, None)


@pytest.fixture
def bwc_mock():
    # ``build_webhook_context`` returns the shape the endpoint uses —
    # we only consume ``assistant`` today, but keep the rest for
    # realism.
    return MagicMock(
        return_value={
            "assistant": FAKE_ASSISTANT,
            "contacts": [],
            "is_valid_contact": False,
            "matched_contact": None,
            "is_job_running": True,
            "job_started": True,
        },
    )


@pytest.fixture
def publish_mock():
    return MagicMock(return_value="msg-id")


class TestTeamsMeetCallStatusEndpoint:

    def _query(self, **overrides):
        params = {
            "assistant_id": "42",
            "livekit_room": "unity_42_teams_meet",
            "conference_name": "Unity_TeamsMeet_42_20260101",
        }
        params.update(overrides)
        return "&".join(f"{k}={v}" for k, v in params.items())

    def test_in_progress_publishes_teams_meet_started(
        self,
        client,
        bwc_mock,
        publish_mock,
    ):
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": "in-progress", "CallSid": "CA_123"},
            )
        assert resp.status_code == 200, resp.text
        publish_mock.assert_called_once()
        kwargs = publish_mock.call_args.kwargs
        assert kwargs["assistant_id"] == "42"
        assert kwargs["thread"] == "teams_meet_started"
        evt = kwargs["event"]
        assert evt["assistant_id"] == "42"
        assert evt["livekit_room"] == "unity_42_teams_meet"
        assert evt["conference_name"] == "Unity_TeamsMeet_42_20260101"
        assert evt["twilio_call_sid"] == "CA_123"
        assert evt["call_status"] == "in-progress"

    def test_contacts_are_boss_only(self, client, bwc_mock, publish_mock):
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": "in-progress", "CallSid": "CA_123"},
            )
        evt = publish_mock.call_args.kwargs["event"]
        assert len(evt["contacts"]) == 1
        boss = evt["contacts"][0]
        assert boss["contact_id"] == 1
        assert boss["email_address"] == "julia@contoso.com"
        assert boss["phone_number"] == "+14155550001"
        # The assistant (contact_id=0) must NEVER appear — Unity's
        # organizer selector picks the first non-boss contact, and we
        # don't want to hand it the assistant entry.
        assert all(c["contact_id"] != 0 for c in evt["contacts"])

    @pytest.mark.parametrize(
        "terminal_status",
        ["completed", "no-answer", "busy", "canceled", "failed"],
    )
    def test_terminal_statuses_publish_teams_meet_ended(
        self,
        client,
        bwc_mock,
        publish_mock,
        terminal_status,
    ):
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": terminal_status, "CallSid": "CA_end"},
            )
        assert resp.status_code == 200
        publish_mock.assert_called_once()
        assert publish_mock.call_args.kwargs["thread"] == "teams_meet_ended"
        assert publish_mock.call_args.kwargs["event"]["call_status"] == terminal_status

    @pytest.mark.parametrize(
        "intermediate_status",
        ["queued", "ringing", "initiated"],
    )
    def test_intermediate_statuses_skip_publish(
        self,
        client,
        bwc_mock,
        publish_mock,
        intermediate_status,
    ):
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": intermediate_status, "CallSid": "CA_mid"},
            )
        assert resp.status_code == 200
        publish_mock.assert_not_called()
        # Don't bother activating the Unity job for noise statuses —
        # the in-progress transition will do that.
        bwc_mock.assert_not_called()

    def test_missing_assistant_id_short_circuits(
        self,
        client,
        bwc_mock,
        publish_mock,
    ):
        qs = "livekit_room=unity__teams_meet" "&conference_name=Unity_TeamsMeet__x"
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{qs}",
                data={"CallStatus": "in-progress", "CallSid": "CA_x"},
            )
        assert resp.status_code == 200
        publish_mock.assert_not_called()
        bwc_mock.assert_not_called()

    def test_activates_unity_job_via_build_webhook_context(
        self,
        client,
        bwc_mock,
        publish_mock,
    ):
        """``build_webhook_context`` is the path that fires ``start_unity_job``.

        Without it the Unity container can idle-recycle mid-meeting
        and drop the ``teams_meet_ended`` event, stranding the
        assistant's ``has_active_teams_meet`` flag set to ``True``
        forever.
        """
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": "in-progress", "CallSid": "CA_1"},
            )
        bwc_mock.assert_called_once()
        kwargs = bwc_mock.call_args.kwargs
        assert kwargs.get("assistant_id") == "42"
        assert kwargs.get("ensure_job") is True
        # Teams-meet has no phone/email counter-party at this stage —
        # skip the phone-matching path.
        assert kwargs.get("validate_contact") is False
        # The channel name is ``teams_meet`` so metrics/logs disambiguate
        # it from the regular phone-call branch.
        args = bwc_mock.call_args.args
        assert args[0] == "teams_meet"

    def test_publish_failure_is_non_fatal(self, client, bwc_mock):
        boom = MagicMock(side_effect=RuntimeError("pubsub down"))
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_mock),
            patch.object(adapters_main, "publish_assistant_event", boom),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": "completed", "CallSid": "CA_z"},
            )
        # The endpoint must not 5xx — Twilio would retry forever and
        # we'd double-publish ``teams_meet_ended`` on every retry.
        assert resp.status_code == 200

    def test_build_webhook_context_failure_is_non_fatal(
        self,
        client,
        publish_mock,
    ):
        bwc_fail = MagicMock(side_effect=RuntimeError("orchestra down"))
        with (
            patch.object(adapters_main, "build_webhook_context", bwc_fail),
            patch.object(adapters_main, "publish_assistant_event", publish_mock),
        ):
            resp = client.post(
                f"/twilio/teams-meet-call-status?{self._query()}",
                data={"CallStatus": "in-progress", "CallSid": "CA_z"},
            )
        assert resp.status_code == 200
        # We didn't manage to resolve the assistant, so we can't
        # trust any contact payload — skip the publish rather than
        # emit a garbled event.
        publish_mock.assert_not_called()
