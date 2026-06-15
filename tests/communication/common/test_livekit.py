"""Unit tests for common/livekit.py — room naming and recording filepath."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

from common.livekit import make_room_name, _start_room_egress
from common.settings import SETTINGS


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


TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}")
ENV_DEFAULTS = {
    "GCP_SA_KEY": "fake-creds",
    "LIVEKIT_EGRESS_GCS_BUCKET": "test-bucket",
    "UNITY_ADAPTERS_URL": "https://adapters.example.com",
    "LIVEKIT_API_KEY": "fake-key",
}


def _mock_livekit_api():
    api = MagicMock()
    api.egress = MagicMock()
    api.egress.start_room_composite_egress = AsyncMock(
        return_value=MagicMock(egress_id="eg-test"),
    )
    return api


def _extract_filepath(mock_api):
    call_args = mock_api.egress.start_room_composite_egress.call_args
    return call_args[0][0].file_outputs[0].filepath


class TestRecordingFilepath:

    def test_filepath_contains_timestamp(self):
        mock_api = _mock_livekit_api()
        with patch.dict("os.environ", ENV_DEFAULTS, clear=False):
            asyncio.run(
                _start_room_egress(mock_api, "unity_25_phone", "25", "user1"),
            )
        filepath = _extract_filepath(mock_api)
        assert TIMESTAMP_RE.search(
            filepath,
        ), f"Filepath should contain a YYYY-MM-DDTHH-MM-SS timestamp: {filepath}"

    def test_filepath_structure_production(self):
        mock_api = _mock_livekit_api()
        env = {**ENV_DEFAULTS, "DEPLOY_ENV": "production"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(SETTINGS, "deploy_env", "production"),
        ):
            asyncio.run(
                _start_room_egress(mock_api, "unity_42_meet", "42", "user2"),
            )
        filepath = _extract_filepath(mock_api)

        assert filepath.startswith(
            "production/42/",
        ), f"Expected production/42/ prefix: {filepath}"
        assert filepath.endswith(".mp3"), f"Expected .mp3 extension: {filepath}"
        assert (
            "unity_42_meet_" in filepath
        ), f"Expected room name followed by underscore before timestamp: {filepath}"

    def test_staging_prefix(self):
        mock_api = _mock_livekit_api()
        env = {**ENV_DEFAULTS, "DEPLOY_ENV": "staging"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(SETTINGS, "deploy_env", "staging"),
        ):
            asyncio.run(
                _start_room_egress(mock_api, "unity_10_phone", "10", "user3"),
            )
        filepath = _extract_filepath(mock_api)

        assert filepath.startswith(
            "staging/10/",
        ), f"Expected staging/10/ prefix when DEPLOY_ENV=staging: {filepath}"

    def test_filepath_pattern_matches_full_format(self):
        """Verify the complete filepath matches {env}/{id}/{room}_{timestamp}.mp3."""
        mock_api = _mock_livekit_api()
        env = {**ENV_DEFAULTS, "DEPLOY_ENV": "production"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(SETTINGS, "deploy_env", "production"),
        ):
            asyncio.run(
                _start_room_egress(mock_api, "unity_25_phone", "25", "user1"),
            )
        filepath = _extract_filepath(mock_api)

        pattern = re.compile(
            r"^production/25/unity_25_phone_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.mp3$",
        )
        assert pattern.match(
            filepath,
        ), f"Filepath doesn't match expected pattern: {filepath}"
