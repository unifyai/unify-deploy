import json
import random
import string
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from communication.helpers import get_twilio_client, get_twilio_wa_client
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

MESSAGING_SERVICE_NAME = "Unity"


# --- Schema ---
class VerificationRequest(BaseModel):
    platform: str = Field(
        ...,
        description="The platform to verify (e.g., 'whatsapp', 'phone').",
    )
    account_identifier: str = Field(
        ...,
        description="The user's account identifier (e.g., phone number).",
    )


# --- Helper Functions ---
def generate_verification_code(length: int = 6) -> str:
    """Generates a random numeric verification code."""
    return "".join(random.choices(string.digits, k=length))


_messaging_service_sid: str | None = None


def _get_messaging_service_sid() -> str:
    """Look up the SID of the 'Unity' Twilio Messaging Service.

    The Messaging Service has a pool of phone numbers across countries,
    so Twilio automatically selects a valid sender for the destination.
    """
    global _messaging_service_sid
    if _messaging_service_sid is not None:
        return _messaging_service_sid
    twilio_client = get_twilio_client()
    for service in twilio_client.messaging.v1.services.list():
        if service.friendly_name == MESSAGING_SERVICE_NAME:
            _messaging_service_sid = service.sid
            return _messaging_service_sid
    raise RuntimeError(
        f"Twilio Messaging Service '{MESSAGING_SERVICE_NAME}' not found",
    )


# --- API Endpoints ---
@router.get("/available-platforms", tags=["Verification"])
async def get_social_platforms():
    """
    Fetches the available social media platforms
    and their respective account creation cost.
    """
    platforms = {"whatsapp": 10.0}
    return {"success": True, "platforms": platforms}


@router.post("/verify", tags=["Verification"])
async def send_verification_message(request: VerificationRequest):
    """
    Triggers a verification for a user account on a given social media platform or phone number.

    This endpoint sends a verification code to the specified account. If the message
    is sent successfully, it returns the code and the sending timestamp.
    """
    platform = request.platform.lower()
    identifier = request.account_identifier
    code = generate_verification_code()

    if platform == "whatsapp":
        try:
            twilio_client = get_twilio_wa_client()
            twilio_client.messages.create(
                content_sid="HX66a14c4ec2f4e8a9d1d14ac2fa439a29",
                content_variables=json.dumps({"1": code}),
                to=f"whatsapp:{identifier}",
                from_=f"whatsapp:+16626772032",
            )
        except Exception as e:
            print(f"ERROR sending WhatsApp verification: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to send WhatsApp verification message.",
            )

    elif platform == "phone":
        message = f"Your Unify verification code is: {code}"
        try:
            twilio_client = get_twilio_client()
            messaging_sid = _get_messaging_service_sid()
            twilio_client.messages.create(
                to=identifier,
                messaging_service_sid=messaging_sid,
                body=message,
            )
        except Exception as e:
            print(f"ERROR sending phone verification: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to send phone verification sms.",
            )

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{platform}' is not supported. Supported platforms are: 'whatsapp', 'phone'.",
        )

    sent_at = datetime.now(timezone.utc).isoformat()

    return {
        "verification_code": code,
        "sent_at": sent_at,
    }
