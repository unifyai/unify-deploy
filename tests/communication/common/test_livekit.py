"""Unit tests for common/livekit.py — room naming.

Recording is not started from this service. The egress request shape and its
refusal rules are covered against the single implementation in
``unify.gateway.common.livekit`` (see ``tests/gateway/common/test_livekit.py``
in the unify repo).
"""

import re

from common.livekit import make_room_name


class TestMakeRoomName:

    def test_phone_medium(self):
        assert make_room_name("25", "phone") == "unity_25_phone"

    def test_meet_medium(self):
        assert make_room_name("42", "meet") == "unity_42_meet"

    def test_teams_medium(self):
        assert make_room_name("7", "teams") == "unity_7_teams"

    def test_hyphenated_assistant_id(self):
        assert (
            make_room_name("default-test-assistant", "phone")
            == "unity_default-test-assistant_phone"
        )

    def test_room_name_is_deterministic(self):
        a = make_room_name("99", "meet")
        b = make_room_name("99", "meet")
        assert a == b

    def test_room_name_format_regex(self):
        """Room names follow the pattern unity_{id}_{medium}."""
        for aid, medium in [("568", "phone"), ("42", "meet"), ("7", "teams")]:
            name = make_room_name(aid, medium)
            assert re.match(
                r"^unity_\w+_(phone|meet|teams)$",
                name,
            ), f"Bad room name format: {name}"

    def test_sip_uri_uses_room_name_not_phone_number(self):
        """SIP URIs built from room names must not contain phone digits."""
        room = make_room_name("568", "phone")
        sip_uri = f"sip:{room}@example.sip.livekit.cloud"
        assert sip_uri == "sip:unity_568_phone@example.sip.livekit.cloud"
        assert "+" not in sip_uri
