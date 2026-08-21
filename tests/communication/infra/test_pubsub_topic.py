"""
Unit tests for the /infra/pubsub/topic create and delete endpoints.

These tests verify:
- Both subscriptions are created with correct filters, and no durable
  subscription is created for a thread nothing pulls
- The "already exists" path updates existing subscriptions and creates only
  the ones that are missing
- Deletion iterates all attached subscriptions, so subscriptions provisioned
  before they stopped being created are still cleaned up
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
    def test_creates_both_subscriptions(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """Both subscriptions should be created on a fresh topic."""
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
        assert len(captured) == 2

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
    def test_unread_threads_get_no_durable_subscription(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """Only inbound and outbound get a durable subscription.

        ``action_event`` and ``system_error`` are read over per-connection
        ephemeral subscriptions the console creates and deletes around each SSE
        connection. A durable one per assistant for those threads is never
        pulled, and at four subscriptions per assistant the project reached its
        10,000-subscription ceiling, which fails every new ephemeral
        subscription and takes chat down.
        """
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        created = {req["name"] for req in captured}
        assert created == {
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-sub",
            "projects/gcp-project-runtime/subscriptions/unity-test-staging-outbound-sub",
        }

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_response_carries_topic_and_inbound_subscription(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """The response body should name the topic and the inbound subscription."""
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
        assert data["subscription_name"].endswith("-sub")
        assert "actions_subscription_name" not in data
        assert "system_error_subscription_name" not in data

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
        assert len(captured) == 2


# =========================================================================
# CREATE — "already exists" path (all subscriptions exist)
# =========================================================================


class TestCreatePubSubTopicAlreadyExists:
    """Tests for POST /infra/pubsub/topic when subscriptions already exist."""

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_updates_existing_subs_when_already_exist(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """When both subscriptions already exist, their expiration policies
        should be updated and neither recreated."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        def always_exists(*, request):
            raise Exception("Resource already exists")

        subscriber.create_subscription.side_effect = always_exists

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # 2 updates: main + outbound
        assert subscriber.update_subscription.call_count == 2
        subscriber.delete_subscription.assert_not_called()

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_creates_only_the_missing_subscription(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """Mixed state: the main sub exists, the outbound one does not. The
        existing sub takes the update path and the missing one is created."""
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
            # Remaining call (outbound) — succeeds
            return MagicMock()

        subscriber.create_subscription.side_effect = create_sub_side_effect

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # Only the main-sub triggered the "already exists" path → 1 update
        assert subscriber.update_subscription.call_count == 1
        # 2 total create_subscription calls: 1 failed + 1 succeeded
        assert subscriber.create_subscription.call_count == 2

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
