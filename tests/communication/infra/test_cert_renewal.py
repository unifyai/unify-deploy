"""
Tests for wildcard TLS certificate auto-renewal.

Verifies:
- check_cert_expiry correctly computes days remaining from a real cert
- check_cert_expiry returns 0 when secret is missing
- renew_if_needed skips renewal when cert has plenty of time left
- renew_if_needed triggers renewal when cert is near expiry
- update_secrets adds new versions to both secrets
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from google.api_core.exceptions import NotFound


def _make_cert_pem(days_valid: int = 90) -> bytes:
    """Generate a self-signed cert PEM that expires in `days_valid` days."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


class TestCheckCertExpiry:

    @patch("communication.infra.cert_renewal.secretmanager.SecretManagerServiceClient")
    def test_returns_days_remaining(self, mock_sm_cls):
        from communication.infra.cert_renewal import check_cert_expiry

        cert_pem = _make_cert_pem(days_valid=60)

        mock_response = MagicMock()
        mock_response.payload.data = cert_pem
        mock_sm_cls.return_value.access_secret_version.return_value = mock_response

        days = check_cert_expiry()
        assert 59 <= days <= 60

    @patch("communication.infra.cert_renewal.secretmanager.SecretManagerServiceClient")
    def test_returns_zero_when_secret_missing(self, mock_sm_cls):
        from communication.infra.cert_renewal import check_cert_expiry

        mock_sm_cls.return_value.access_secret_version.side_effect = NotFound(
            "not found",
        )

        days = check_cert_expiry()
        assert days == 0

    @patch("communication.infra.cert_renewal.secretmanager.SecretManagerServiceClient")
    def test_returns_zero_on_parse_error(self, mock_sm_cls):
        from communication.infra.cert_renewal import check_cert_expiry

        mock_response = MagicMock()
        mock_response.payload.data = b"not a cert"
        mock_sm_cls.return_value.access_secret_version.return_value = mock_response

        days = check_cert_expiry()
        assert days == 0


class TestRenewIfNeeded:

    @patch("communication.infra.cert_renewal.check_cert_expiry", return_value=75)
    def test_skips_renewal_when_not_near_expiry(self, _mock_expiry):
        from communication.infra.cert_renewal import renew_if_needed

        result = renew_if_needed(days_threshold=30)

        assert result["renewed"] is False
        assert result["days_remaining"] == 75

    @patch("communication.infra.cert_renewal.update_secrets")
    @patch("communication.infra.cert_renewal.perform_dns01_renewal")
    @patch("communication.infra.cert_renewal.check_cert_expiry")
    def test_triggers_renewal_when_near_expiry(
        self,
        mock_expiry,
        mock_renew,
        mock_update,
    ):
        from communication.infra.cert_renewal import renew_if_needed

        mock_expiry.side_effect = [10, 90]
        mock_renew.return_value = ("--fullchain--", "--privkey--")

        result = renew_if_needed(days_threshold=30)

        assert result["renewed"] is True
        assert result["days_remaining"] == 90
        mock_renew.assert_called_once()
        mock_update.assert_called_once_with("--fullchain--", "--privkey--")

    @patch("communication.infra.cert_renewal.update_secrets")
    @patch("communication.infra.cert_renewal.perform_dns01_renewal")
    @patch("communication.infra.cert_renewal.check_cert_expiry")
    def test_triggers_renewal_when_cert_missing(
        self,
        mock_expiry,
        mock_renew,
        mock_update,
    ):
        from communication.infra.cert_renewal import renew_if_needed

        mock_expiry.side_effect = [0, 90]
        mock_renew.return_value = ("--fullchain--", "--privkey--")

        result = renew_if_needed(days_threshold=30)

        assert result["renewed"] is True
        mock_renew.assert_called_once()


class TestUpdateSecrets:

    @patch("communication.infra.cert_renewal.secretmanager.SecretManagerServiceClient")
    def test_adds_new_versions_to_both_secrets(self, mock_sm_cls):
        from communication.infra.cert_renewal import update_secrets

        update_secrets("--fullchain--", "--privkey--")

        calls = mock_sm_cls.return_value.add_secret_version.call_args_list
        assert len(calls) == 2

        first_call = calls[0].kwargs["request"]
        assert "VM_WILDCARD_FULLCHAIN" in first_call["parent"]
        assert first_call["payload"]["data"] == b"--fullchain--"

        second_call = calls[1].kwargs["request"]
        assert "VM_WILDCARD_PRIVKEY" in second_call["parent"]
        assert second_call["payload"]["data"] == b"--privkey--"
