"""Unit tests for Twilio signature validation with forwarded headers.

The validate_twilio_signature dependency must reconstruct the public URL
from X-Forwarded-Proto/Host headers, because Cloud Run rewrites the Host
header internally.
"""

from fastapi import FastAPI, Depends, Request
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

TWILIO_AUTH_TOKEN = "test_auth_token_for_validation"

app = FastAPI()


# Import and wire the real validator from adapters, forcing it to use the
# test token rather than whatever is in the real environment.
import adapters.main as _adapters_mod

_adapters_mod._twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
from adapters.main import validate_twilio_signature


@app.post("/test-webhook", dependencies=[Depends(validate_twilio_signature)])
async def _test_endpoint(request: Request):
    return {"ok": True}


client = TestClient(app, raise_server_exceptions=False)
_validator = RequestValidator(TWILIO_AUTH_TOKEN)


def _sign(url: str, params: dict) -> str:
    """Generate a valid Twilio signature for the given URL and params."""
    return _validator.compute_signature(url, params)


class TestTwilioSignatureValidation:

    def test_accepts_correctly_signed_request(self):
        """A request signed with the correct URL and params passes."""
        public_url = "https://droid-adapters-staging.example.com/test-webhook"
        params = {"From": "+1234567890", "To": "+0987654321"}
        sig = _sign(public_url, params)

        resp = client.post(
            "/test-webhook",
            data=params,
            headers={
                "X-Twilio-Signature": sig,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "droid-adapters-staging.example.com",
            },
        )
        assert resp.status_code == 200, resp.text

    def test_rejects_missing_signature(self):
        resp = client.post(
            "/test-webhook",
            data={"From": "+1"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "example.com",
            },
        )
        assert resp.status_code == 403

    def test_rejects_wrong_signature(self):
        resp = client.post(
            "/test-webhook",
            data={"From": "+1"},
            headers={
                "X-Twilio-Signature": "bad_signature",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "example.com",
            },
        )
        assert resp.status_code == 403

    def test_reconstructs_url_from_forwarded_headers(self):
        """Even if the internal Host header differs, forwarded headers win."""
        public_url = "https://my-public-host.example.com/test-webhook"
        params = {"CallSid": "CA123"}
        sig = _sign(public_url, params)

        resp = client.post(
            "/test-webhook",
            data=params,
            headers={
                "X-Twilio-Signature": sig,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "my-public-host.example.com",
                "Host": "internal-host:8080",
            },
        )
        assert resp.status_code == 200, resp.text
