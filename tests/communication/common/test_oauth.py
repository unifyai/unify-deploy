"""Tests for common.oauth — OAuth state signing verification."""

import base64
import hashlib
import hmac
import json

import pytest

from common.oauth import OAuthStateError, verify_oauth_state

SIGNING_KEY = "test-signing-key-abc123"


def _sign_state(payload: dict, key: str = SIGNING_KEY) -> str:
    """Build a signed, base64url-encoded state string (mirrors Orchestra's logic)."""
    canonical = json.dumps(payload, sort_keys=True)
    sig = hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    full = {**payload, "_sig": sig}
    return base64.urlsafe_b64encode(json.dumps(full).encode()).decode()


class TestVerifyOAuthState:
    def test_valid_signature(self):
        payload = {"assistant_id": 42, "provider": "gmail", "byod": True}
        encoded = _sign_state(payload)

        result = verify_oauth_state(encoded, SIGNING_KEY)

        assert result == payload
        assert "_sig" not in result

    def test_tampered_payload_rejected(self):
        payload = {"assistant_id": 42, "provider": "gmail", "byod": True}
        encoded = _sign_state(payload)

        raw = json.loads(base64.urlsafe_b64decode(encoded))
        raw["assistant_id"] = 999
        tampered = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()

        with pytest.raises(OAuthStateError, match="Invalid state signature"):
            verify_oauth_state(tampered, SIGNING_KEY)

    def test_wrong_key_rejected(self):
        payload = {"assistant_id": 42, "provider": "gmail", "byod": True}
        encoded = _sign_state(payload, key="correct-key")

        with pytest.raises(OAuthStateError, match="Invalid state signature"):
            verify_oauth_state(encoded, "wrong-key")

    def test_missing_signature_rejected(self):
        payload = {"assistant_id": 42, "provider": "gmail", "byod": True}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

        with pytest.raises(OAuthStateError, match="Missing state signature"):
            verify_oauth_state(encoded, SIGNING_KEY)

    def test_garbage_base64_rejected(self):
        with pytest.raises(OAuthStateError, match="Invalid state parameter"):
            verify_oauth_state("not-valid-json!!!", SIGNING_KEY)

    def test_non_json_base64_rejected(self):
        encoded = base64.urlsafe_b64encode(b"just a plain string").decode()
        with pytest.raises(OAuthStateError, match="Invalid state parameter"):
            verify_oauth_state(encoded, SIGNING_KEY)

    def test_extra_fields_preserved(self):
        payload = {
            "assistant_id": 7,
            "provider": "microsoft",
            "byod": True,
            "redirect_after": "https://app.test/done",
        }
        encoded = _sign_state(payload)

        result = verify_oauth_state(encoded, SIGNING_KEY)
        assert result["redirect_after"] == "https://app.test/done"

    def test_empty_string_signature_rejected(self):
        """A state with _sig set to empty string should be rejected."""
        payload = {"assistant_id": 42, "_sig": ""}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

        with pytest.raises(OAuthStateError, match="Missing state signature"):
            verify_oauth_state(encoded, SIGNING_KEY)
