import os
from twilio.rest import Client as TwilioClient

STAGING = os.getenv("STAGING")
ORCHESTRA_URL = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://service.a.run.app/v0"
)

def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)