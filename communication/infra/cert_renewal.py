"""
Wildcard TLS Certificate Auto-Renewal

Renews the *.vm.unify.ai wildcard certificate via Let's Encrypt DNS-01
challenge and updates the corresponding Secret Manager secrets so that
all future VMs receive the fresh cert through instance metadata.

Uses the `acme` Python library (standalone ACME protocol client) with
Google Cloud DNS for challenge record management.
"""

import logging
import time
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.cloud import dns, secretmanager
from google.api_core.exceptions import NotFound

from .vm_config import (
    VM_PROJECT_ID,
    DNS_PROJECT_ID,
    DNS_ZONE_NAME,
    VM_WILDCARD_CERT_SECRET,
    VM_WILDCARD_KEY_SECRET,
)

logger = logging.getLogger(__name__)

WILDCARD_DOMAIN = "*.vm.unify.ai"
ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
ACME_CONTACT = "infra@unify.ai"
DNS_PROPAGATION_SECONDS = 60
CHALLENGE_RECORD_NAME = "_acme-challenge.vm.unify.ai."


def check_cert_expiry() -> int:
    """Return days until the current wildcard cert expires.

    Returns 0 if the secret doesn't exist or can't be parsed.
    """
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = (
            f"projects/{VM_PROJECT_ID}/secrets/"
            f"{VM_WILDCARD_CERT_SECRET}/versions/latest"
        )
        response = client.access_secret_version(request={"name": name})
        pem_data = response.payload.data

        cert = x509.load_pem_x509_certificate(pem_data)
        now = datetime.now(timezone.utc)
        delta = cert.not_valid_after_utc - now
        return max(0, delta.days)
    except NotFound:
        logger.info("Wildcard cert secret not found")
        return 0
    except Exception as e:
        logger.warning(f"Failed to check cert expiry: {e}")
        return 0


def _create_acme_client():
    """Create and register an ACME client with Let's Encrypt."""
    import josepy
    from acme import client as acme_client, messages
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    acme_key = josepy.JWKRSA(key=key)
    net = acme_client.ClientNetwork(acme_key)
    directory = messages.Directory.from_json(net.get(ACME_DIRECTORY).json())
    acme = acme_client.ClientV2(directory, net)

    registration = acme.new_account(
        messages.NewRegistration.from_data(
            email=ACME_CONTACT,
            terms_of_service_agreed=True,
        ),
    )
    logger.info(f"ACME account registered: {registration.uri}")
    return acme


def _set_dns_txt_record(value: str) -> None:
    """Create the _acme-challenge TXT record in Cloud DNS."""
    client = dns.Client(project=DNS_PROJECT_ID)
    zone = client.zone(DNS_ZONE_NAME)

    # Delete existing TXT record if present
    try:
        for record in zone.list_resource_record_sets():
            if record.name == CHALLENGE_RECORD_NAME and record.record_type == "TXT":
                changes = zone.changes()
                changes.delete_record_set(record)
                changes.create()
                logger.info("Deleted existing ACME challenge TXT record")
                break
    except Exception as e:
        logger.warning(f"Could not check existing TXT records: {e}")

    record_set = zone.resource_record_set(
        CHALLENGE_RECORD_NAME,
        "TXT",
        60,
        [f'"{value}"'],
    )
    changes = zone.changes()
    changes.add_record_set(record_set)
    changes.create()
    logger.info(f"Created ACME challenge TXT record: {value[:20]}...")


def _delete_dns_txt_record() -> None:
    """Remove the _acme-challenge TXT record after validation."""
    try:
        client = dns.Client(project=DNS_PROJECT_ID)
        zone = client.zone(DNS_ZONE_NAME)
        for record in zone.list_resource_record_sets():
            if record.name == CHALLENGE_RECORD_NAME and record.record_type == "TXT":
                changes = zone.changes()
                changes.delete_record_set(record)
                changes.create()
                logger.info("Cleaned up ACME challenge TXT record")
                return
    except Exception as e:
        logger.warning(f"Failed to clean up TXT record: {e}")


def perform_dns01_renewal() -> tuple[str, str]:
    """Request a new wildcard cert via ACME DNS-01 challenge.

    Returns (fullchain_pem, privkey_pem) as strings.
    """
    from acme import challenges

    acme = _create_acme_client()

    # Generate a new private key for the certificate
    cert_key = ec.generate_private_key(ec.SECP256R1())
    cert_key_pem = cert_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    # Create CSR
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(WILDCARD_DOMAIN)],
            ),
            critical=False,
        )
        .sign(cert_key, hashes.SHA256())
    )

    # Request the order
    order = acme.new_order(csr.public_bytes(serialization.Encoding.PEM))
    logger.info(f"ACME order created: {order.uri}")

    # Find and respond to the DNS-01 challenge
    for authz in order.authorizations:
        for challenge in authz.body.challenges:
            if isinstance(challenge.chall, challenges.DNS01):
                response, validation = challenge.response_and_validation(
                    acme.net.key,
                )

                _set_dns_txt_record(validation)

                logger.info(
                    f"Waiting {DNS_PROPAGATION_SECONDS}s for DNS propagation...",
                )
                time.sleep(DNS_PROPAGATION_SECONDS)

                acme.answer_challenge(challenge, response)
                break

    # Finalize and download the certificate
    order = acme.poll_and_finalize(order)
    fullchain_pem = order.fullchain_pem
    logger.info("Certificate issued successfully")

    _delete_dns_txt_record()

    return fullchain_pem, cert_key_pem


def update_secrets(fullchain: str, privkey: str) -> None:
    """Add new versions to the wildcard cert secrets in Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()

    for secret_name, payload in [
        (VM_WILDCARD_CERT_SECRET, fullchain),
        (VM_WILDCARD_KEY_SECRET, privkey),
    ]:
        parent = f"projects/{VM_PROJECT_ID}/secrets/{secret_name}"
        client.add_secret_version(
            request={
                "parent": parent,
                "payload": {"data": payload.encode("utf-8")},
            },
        )
        logger.info(f"Updated secret: {secret_name}")


def renew_if_needed(days_threshold: int = 30) -> dict:
    """Check cert expiry and renew if within threshold.

    Returns a status dict suitable for the HTTP response.
    """
    days_remaining = check_cert_expiry()

    if days_remaining > days_threshold:
        logger.info(
            f"Wildcard cert has {days_remaining} days remaining, "
            f"skipping renewal (threshold={days_threshold})",
        )
        return {
            "renewed": False,
            "days_remaining": days_remaining,
            "message": f"Cert valid for {days_remaining} more days",
        }

    if days_remaining > 0:
        logger.info(
            f"Wildcard cert expires in {days_remaining} days, renewing...",
        )
    else:
        logger.info("Wildcard cert missing or expired, issuing new cert...")

    fullchain, privkey = perform_dns01_renewal()
    update_secrets(fullchain, privkey)

    # Push renewed cert to all running pool VMs
    from .vm_helpers import push_cert_to_pool_vms

    push_result = push_cert_to_pool_vms()
    logger.info(f"Cert pushed to {len(push_result.get('vms', []))} running pool VMs")

    new_days = check_cert_expiry()
    logger.info(f"Renewal complete, new cert valid for {new_days} days")

    return {
        "renewed": True,
        "days_remaining": new_days,
        "message": f"Cert renewed, valid for {new_days} days",
        "push_result": push_result,
    }
