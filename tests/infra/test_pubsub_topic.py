"""
Unit tests for the /infra/pubsub/topic create and delete endpoints.

These tests verify:
- All three subscriptions are created with correct filters
- The actions subscription has 10-minute message retention
- The "already exists" path updates existing subscriptions and creates the
  actions subscription if it is missing (migration from older assistants)
- Deletion iterates all attached subscriptions (no code change needed, but
  we verify the existing behaviour still holds with the new subscription)
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

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


# =========================================================================
# CREATE — fresh assistant (no subscriptions exist yet)
# =========================================================================


class TestCreatePubSubTopic:
    """Tests for POST /infra/pubsub/topic — fresh creation path."""

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.SubscriberClient")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict("os.environ", {"GCP_SA_KEY": GCP_SA_KEY_JSON})
    def test_creates_all_three_subscriptions(
        self,
        mock_pub_cls,
        mock_sub_cls,
        mock_creds,
        client,
    ):
        """All three subscriptions should be created on a fresh topic."""
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
        assert len(captured) == 3

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
        """The main -sub should filter OUT both outbound and action_event messages."""
        publisher, subscriber, captured = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        client.post("/infra/pubsub/topic", data={"topic_name": "unity-test-staging"})

        req = captured[0]
        expected_filter = (
            'NOT attributes.thread = "unify_message_outbound"'
            ' AND NOT attributes.thread = "action_event"'
        )
        assert req["filter"] == expected_filter
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

        req = captured[1]
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

        req = captured[2]
        assert req["filter"] == 'attributes.thread = "action_event"'
        assert req["name"].endswith("-actions-sub")

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

        req = captured[2]
        retention = req["message_retention_duration"]
        assert (
            retention.seconds == 1800
        ), f"Expected 1800s (30 min) retention, got {retention.seconds}s"

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
        assert len(captured) == 3


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
        """When the first create_subscription fails with 'already exists',
        existing subscriptions should be updated and the actions sub should
        be attempted as a create (then updated if it also already exists)."""
        publisher, subscriber, _ = _setup_pubsub_mocks(
            mock_pub_cls,
            mock_sub_cls,
            mock_creds,
        )

        # Every create_subscription call raises "already exists"
        def always_exists(*, request):
            raise Exception("Resource already exists")

        subscriber.create_subscription.side_effect = always_exists

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # 2 updates for main + outbound, 1 update for actions (after failed create)
        assert subscriber.update_subscription.call_count == 3

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
                # First call (main-sub in try block) — already exists
                raise Exception("Resource already exists")
            # Second call (actions-sub in except block) — succeeds
            return MagicMock()

        subscriber.create_subscription.side_effect = create_sub_side_effect

        response = client.post(
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        # main + outbound updated, actions created (not updated)
        assert subscriber.update_subscription.call_count == 2
        # 2 total create_subscription calls: first (failed) + actions (succeeded)
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
            Exception("Subscription not found"),
        ]

        response = client.request(
            "DELETE",
            "/infra/pubsub/topic",
            data={"topic_name": "unity-test-staging"},
        )

        assert response.status_code == 200
        assert subscriber.delete_subscription.call_count == 2
        publisher.delete_topic.assert_called_once()
