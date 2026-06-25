"""
Unit tests for the /infra/pubsub/topic create and delete endpoints.

These tests verify:
- All four subscriptions are created with correct filters
- The actions subscription has 30-minute message retention
- The actions subscription has message ordering enabled
- The "already exists" path updates existing subscriptions and creates the
  actions subscription if it is missing (migration from older assistants)
- Existing actions subscriptions without ordering are deleted and recreated
  (enable_message_ordering cannot be updated on an existing subscription)
- Deletion iterates all attached subscriptions (no code change needed, but
  we verify the existing behaviour still holds with the new subscription)
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound as GcpNotFound

import communication.infra.runtime_clients as _runtime_clients_mod
import communication.infra.views as _views_mod
from common.settings import SETTINGS

# Keep legacy patch targets pointing at the shared runtime client module objects.
_views_mod.Credentials = _runtime_clients_mod.Credentials
_views_mod.pubsub_v1 = _runtime_clients_mod.pubsub_v1

GCP_SA_KEY_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "1",
        "private_key": "key",
        "client_email": "service-account@example.iam.gserviceaccount.com",
        "client_id": "1",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    },
)


@pytest.fixture
def client():
    """Create a FastAPI test client for the infra router."""
    from fastapi import FastAPI
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


# =========================================================================
# Helper to set up common mocks
# =========================================================================


def _setup_pubsub_mocks(mock_publisher_class, mock_subscriber_class, mock_creds):
    """Return configured (publisher, subscriber) mock instances and a list
    that captures a snapshot of each create_subscription request dict.

    The production code mutates a single request dict, so all
    ``call_args_list`` entries end up pointing to the same (final) object.
    We install a ``side_effect`` that shallow-copies each request dict at
    call time so individual assertions work correctly.
    """
    _runtime_clients_mod._pubsub_publisher = None
    _runtime_clients_mod._pubsub_subscriber = None

    mock_creds.return_value = MagicMock()

    publisher = MagicMock()
    mock_publisher_class.return_value = publisher
    publisher.topic_path.return_value = (
        "projects/gcp-project-runtime/topics/unity-test-staging"
    )

    subscriber = MagicMock()
    mock_subscriber_class.return_value = subscriber
    subscriber.subscription_path.side_effect = lambda project, name: (
        f"projects/{project}/subscriptions/{name}"
    )

    captured_requests: list[dict] = []

    def _capture_create(*, request):
        captured_requests.append({k: v for k, v in request.items()})
        return MagicMock()

    subscriber.create_subscription.side_effect = _capture_create

    return publisher, subscriber, captured_requests


def _request_for_subscription(captured_requests: list[dict], subscription: str) -> dict:
    matches = [
        request
        for request in captured_requests
        if str(request["name"]).split("/")[-1] == subscription
    ]
    assert len(matches) == 1
    return matches[0]


# =========================================================================
# CREATE — fresh assistant (no subscriptions exist yet)
# =========================================================================


class TestCreatePubSubTopic:
    """Tests for POST /infra/pubsub/topic — fresh creation path."""

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_creates_all_four_subscriptions(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """All four subscriptions should be created on a fresh topic."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        assert len(captured) == 4

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_main_sub_has_correct_filter(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The main -sub should filter FOR inbound messages."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-sub")
        assert req["filter"] == 'attributes.thread = "inbound"'
        assert req["name"].endswith("-sub")
        assert not req["name"].endswith("-outbound-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_outbound_sub_has_correct_filter(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The -outbound-sub should filter FOR outbound messages."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-outbound-sub")
        assert req["filter"] == 'attributes.thread = "unify_message_outbound"'
        assert req["name"].endswith("-outbound-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_actions_sub_has_correct_filter(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The -actions-sub should filter FOR action_event messages."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-actions-sub")
        assert req["filter"] == 'attributes.thread = "action_event"'
        assert req["name"].endswith("-actions-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_actions_sub_has_message_ordering_enabled(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The -actions-sub should have message ordering enabled so events
        are delivered in publish order."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-actions-sub")
        assert req.get("enable_message_ordering") is True

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_actions_sub_has_30min_message_retention(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The -actions-sub should have a 30-minute message retention."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-actions-sub")
        retention = req["message_retention_duration"]
        assert (
            retention.seconds == 1800
        ), f"Expected 1800s (30 min) retention, got {retention.seconds}s"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_system_error_sub_has_correct_filter(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The -system-error-sub should filter FOR system_error messages."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = _request_for_subscription(captured, "unity-test-staging-system-error-sub")
        assert req["filter"] == 'attributes.thread = "system_error"'
        assert req["name"].endswith("-system-error-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_response_includes_system_error_subscription_name(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The response body should include the system error subscription path."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        data = response.json()
        assert data["success"] is True
        assert "system_error_subscription_name" in data
        assert data["system_error_subscription_name"].endswith("-system-error-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_response_includes_actions_subscription_name(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The response body should include the actions subscription path."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        data = response.json()
        assert data["success"] is True
        assert "actions_subscription_name" in data
        assert data["actions_subscription_name"].endswith("-actions-sub")

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_topic_already_exists_is_ignored(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """If the topic already exists, creation should continue without error."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )
        publisher.create_topic.side_effect = Exception("Topic already exists")

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        assert len(captured) == 4


# =========================================================================
# CREATE — "already exists" path (all subscriptions exist)
# =========================================================================


class TestCreatePubSubTopicAlreadyExists:
    """Tests for POST /infra/pubsub/topic when subscriptions already exist."""

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_updates_existing_subs_when_already_exist_and_ordered(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """When all subscriptions already exist and the actions sub already
        has ordering enabled, expiration policies should be updated without
        recreating the actions subscription."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        def always_exists(*, request):
            raise Exception("Resource already exists")

        subscriber.create_subscription.side_effect = always_exists

        # Actions sub already has ordering — no delete+recreate needed
        existing_sub = MagicMock()
        existing_sub.enable_message_ordering = True
        subscriber.get_subscription.return_value = existing_sub

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # 4 updates: main + outbound + actions (expiration only) + system-error
        assert subscriber.update_subscription.call_count == 4
        subscriber.delete_subscription.assert_not_called()

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_recreates_actions_sub_when_ordering_not_enabled(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """When the actions sub exists but lacks message ordering, it should
        be deleted and recreated with ordering enabled. This is necessary
        because enable_message_ordering cannot be updated on an existing sub."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        seen_names = set()

        def create_side_effect(*, request):
            name = request.get("name", "")
            if name not in seen_names:
                # First attempt for every subscription — already exists
                seen_names.add(name)
                raise Exception("Resource already exists")
            # Second attempt (recreate after delete) — succeeds
            return MagicMock()

        subscriber.create_subscription.side_effect = create_side_effect

        # Actions sub exists WITHOUT ordering — must delete and recreate
        existing_sub = MagicMock()
        existing_sub.enable_message_ordering = False
        subscriber.get_subscription.return_value = existing_sub

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # main + outbound + system-error updated (actions was recreated, not updated)
        assert subscriber.update_subscription.call_count == 3
        subscriber.delete_subscription.assert_called_once()
        # 5 total creates: 4 initial (all failed) + 1 actions-sub recreate (succeeded)
        assert subscriber.create_subscription.call_count == 5

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_creates_actions_sub_when_only_old_subs_exist(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """Migration path: main-sub and outbound-sub exist, but actions-sub
        does not. The except block should update the two existing subs and
        successfully create the actions sub."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        call_count = {"n": 0}

        def create_sub_side_effect(*, request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call (main-sub) — already exists
                raise Exception("Resource already exists")
            # Remaining calls (outbound, actions, system-error) — succeed
            return MagicMock()

        subscriber.create_subscription.side_effect = create_sub_side_effect

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # Only the main-sub triggered the "already exists" path → 1 update
        assert subscriber.update_subscription.call_count == 1
        # 4 total create_subscription calls: 1 failed + 3 succeeded
        assert subscriber.create_subscription.call_count == 4

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_non_already_exists_error_propagates(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """A non-'already exists' error should propagate as a 500."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        def permission_denied(*, request):
            raise Exception("Permission denied")

        subscriber.create_subscription.side_effect = permission_denied

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 500
        assert "Permission denied" in response.json()["detail"]


# =========================================================================
# DELETE — verify existing deletion logic handles all subscriptions
# =========================================================================


class TestDeletePubSubTopic:
    """Tests for DELETE /infra/pubsub/topic.

    The delete endpoint iterates all subscriptions attached to the topic
    and deletes them. No code change was needed, but we verify the existing
    behaviour correctly handles the new actions subscription.
    """

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_deletes_all_subscriptions_then_topic(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """All subscriptions (including actions-sub) should be deleted
        before the topic itself is deleted."""
        _runtime_clients_mod._pubsub_publisher = None
        _runtime_clients_mod._pubsub_subscriber = None

        mock_creds.return_value = MagicMock()

        publisher = MagicMock()
        mock_pub_cls.return_value = publisher
        publisher.topic_path.return_value = (
            "projects/gcp-project-runtime/topics/unity-test-staging"
        )

        subscriber = MagicMock()
        mock_sub_cls.return_value = subscriber

        # Simulate three subscriptions attached to the topic
        publisher.list_topic_subscriptions.return_value = [
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-sub",
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-outbound-sub",
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-actions-sub",
        ]

        response = client.request(
            "DELETE",
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        assert subscriber.delete_subscription.call_count == 3
        publisher.delete_topic.assert_called_once()

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_delete_handles_missing_subscription_gracefully(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """If a subscription was already deleted, deletion should continue."""
        _runtime_clients_mod._pubsub_publisher = None
        _runtime_clients_mod._pubsub_subscriber = None

        mock_creds.return_value = MagicMock()

        publisher = MagicMock()
        mock_pub_cls.return_value = publisher
        publisher.topic_path.return_value = (
            "projects/gcp-project-runtime/topics/unity-test-staging"
        )

        subscriber = MagicMock()
        mock_sub_cls.return_value = subscriber

        publisher.list_topic_subscriptions.return_value = [
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-sub",
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-actions-sub",
        ]
        # First delete succeeds, second raises "not found"
        subscriber.delete_subscription.side_effect = [
            None,
            GcpNotFound("Subscription not found"),
        ]

        response = client.request(
            "DELETE",
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        assert subscriber.delete_subscription.call_count == 2
        publisher.delete_topic.assert_called_once()


# =========================================================================
# WAKE — _ensure_assistant_topic_on_wake self-heals a missing topic
# =========================================================================


class TestEnsureAssistantTopicOnWake:
    """Tests for the wake-time topic guard used by ``/infra/job/start``.

    A missing assistant topic permanently dead-ends a wake, so the guard
    re-creates it. The common case (topic present) must stay cheap: a single
    ``get_topic`` and no subscription churn. Failures must never propagate.
    """

    @pytest.mark.asyncio
    async def test_existing_topic_skips_full_ensure(self):
        """When the topic already exists, the full create+subscribe path is
        not invoked (single get_topic, no recreation)."""
        publisher = MagicMock()
        publisher.topic_path.return_value = (
            "projects/gcp-project-runtime/topics/unity-2105-staging"
        )
        publisher.get_topic.return_value = MagicMock()

        with (
            patch(
                "communication.infra.views._get_pubsub_clients",
                return_value=(publisher, MagicMock()),
            ),
            patch(
                "communication.infra.views._ensure_topic_and_subscriptions",
            ) as mock_ensure,
        ):
            await _views_mod._ensure_assistant_topic_on_wake("2105")

        publisher.get_topic.assert_called_once()
        mock_ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_topic_triggers_full_ensure(self):
        """When the topic is missing, the full create+subscribe path runs for
        the assistant's topic name."""
        publisher = MagicMock()
        publisher.topic_path.return_value = (
            "projects/gcp-project-runtime/topics/unity-2105-staging"
        )
        publisher.get_topic.side_effect = GcpNotFound("Topic not found")

        expected_topic = SETTINGS.assistant_topic("2105")

        with (
            patch(
                "communication.infra.views._get_pubsub_clients",
                return_value=(publisher, MagicMock()),
            ),
            patch(
                "communication.infra.views._ensure_topic_and_subscriptions",
                new=AsyncMock(return_value={}),
            ) as mock_ensure,
        ):
            await _views_mod._ensure_assistant_topic_on_wake("2105")

        mock_ensure.assert_awaited_once_with(expected_topic)

    @pytest.mark.asyncio
    async def test_ensure_failure_is_swallowed(self):
        """A failure while ensuring the topic must not propagate (the wake
        proceeds; the failure is observability-only)."""
        publisher = MagicMock()
        publisher.topic_path.return_value = (
            "projects/gcp-project-runtime/topics/unity-2105-staging"
        )
        publisher.get_topic.side_effect = GcpNotFound("Topic not found")

        with (
            patch(
                "communication.infra.views._get_pubsub_clients",
                return_value=(publisher, MagicMock()),
            ),
            patch(
                "communication.infra.views._ensure_topic_and_subscriptions",
                new=AsyncMock(side_effect=RuntimeError("pubsub admin unavailable")),
            ),
        ):
            # Must not raise.
            await _views_mod._ensure_assistant_topic_on_wake("2105")
