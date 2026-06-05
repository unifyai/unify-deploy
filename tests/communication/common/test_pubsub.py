"""Tests for ``common.pubsub.publish_assistant_event``."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from common.pubsub import publish_assistant_event


class TestPublishAssistantEvent:
    def test_publishes_expected_envelope(self):
        publisher = MagicMock()
        publisher.topic_path.return_value = "projects/p/topics/unity-42"
        future = MagicMock()
        future.result.return_value = "msg-id-xyz"
        publisher.publish.return_value = future

        with patch(
            "common.pubsub.get_pubsub_publisher",
            return_value=publisher,
        ):
            msg_id = publish_assistant_event(
                assistant_id="42",
                thread="teams_meet",
                event={"foo": "bar", "n": 1},
            )

        assert msg_id == "msg-id-xyz"
        publisher.publish.assert_called_once()
        args, kwargs = publisher.publish.call_args
        assert args[0] == "projects/p/topics/unity-42"
        assert kwargs["thread"] == "teams_meet"
        body = json.loads(kwargs["data"].decode("utf-8"))
        assert body["thread"] == "teams_meet"
        assert body["event"] == {"foo": "bar", "n": 1}
        assert isinstance(body["publish_timestamp"], float)
