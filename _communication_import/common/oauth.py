"""OAuth state parameter verification.

Orchestra signs the OAuth ``state`` parameter with HMAC-SHA256 before
redirecting users to Google/Microsoft consent screens.  The callbacks in
the adapters service use :func:`verify_oauth_state` to decode the
base64url-encoded state, verify the signature, and return the payload.
"""

import base64
import hashlib
import hmac
import json
import logging

logger = logging.getLogger(__name__)


class OAuthStateError(Exception):
    """Raised when the OAuth state parameter is invalid or tampered."""


def verify_oauth_state(encoded_state: str, signing_key: str) -> dict:
    """Decode and verify a signed OAuth state parameter.

    Args:
        encoded_state: Base64url-encoded JSON state from the query string.
        signing_key: HMAC-SHA256 key (must match the key Orchestra used to sign).

    Returns:
        The verified state payload (``_sig`` removed).

    Raises:
        OAuthStateError: If decoding fails, the signature is missing, or
            the signature does not match.
    """
    try:
        state_bytes = base64.urlsafe_b64decode(encoded_state)
        state_data: dict = json.loads(state_bytes.decode())
    except Exception as exc:
        raise OAuthStateError("Invalid state parameter") from exc

    provided_sig = state_data.pop("_sig", "")
    if not provided_sig:
        raise OAuthStateError("Missing state signature")

    canonical = json.dumps(state_data, sort_keys=True)
    expected_sig = hmac.new(
        signing_key.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(provided_sig, expected_sig):
        raise OAuthStateError("Invalid state signature")

    return state_data
