import os
import random
import string
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from communication.phone.views import get_twilio_client

load_dotenv()

router = APIRouter()


# --- Schema ---
class VerificationRequest(BaseModel):
    platform: str = Field(
        ..., description="The platform to verify (e.g., 'whatsapp', 'phone')."
    )
    account_identifier: str = Field(
        ..., description="The user's account identifier (e.g., phone number)."
    )


# --- Helper Functions ---
def generate_verification_code(length: int = 6) -> str:
    """Generates a random numeric verification code."""
    return "".join(random.choices(string.digits, k=length))


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
        message = f"Your Unify verification code is: {code}"
        try:
            twilio_client = get_twilio_client()

            from_number = os.getenv("TWILIO_VERIFICATION_NUMBER")
            if not from_number:
                raise HTTPException(
                    status_code=500,
                    detail="TWILIO_VERIFICATION_NUMBER environment variable is not configured.",
                )

            twilio_client.messages.create(
                to=f"whatsapp:{identifier}",
                from_=f"whatsapp:{from_number}",
                body=message,
            )
        except Exception as e:
            # Log the full error for debugging but return a generic message to the user
            print(f"ERROR sending WhatsApp verification: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to send WhatsApp verification message."
            )

    elif platform == "phone":
        message = f"Your Unify verification code is: {code}"
        try:
            twilio_client = get_twilio_client()

            from_number = os.getenv("TWILIO_VERIFICATION_NUMBER")
            if not from_number:
                raise HTTPException(
                    status_code=500,
                    detail="TWILIO_VERIFICATION_NUMBER environment variable is not configured.",
                )

            twilio_client.messages.create(
                to=identifier,
                from_=from_number,
                body=message,
            )
        except Exception as e:
            print(f"ERROR sending phone verification: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to send phone verification sms."
            )

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{platform}' is not supported. Supported platforms are: 'whatsapp', 'phone'.",
        )

    # If sending was successful, return the code and a UTC timestamp.
    sent_at = datetime.now(timezone.utc).isoformat()

    return {
        "verification_code": code,
        "sent_at": sent_at,
    }
