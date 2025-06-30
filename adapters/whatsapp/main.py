import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
import requests
from twilio.twiml.messaging_response import MessagingResponse


def get_assistant_and_voice_info(
    email_id: str = None,
    phone_number: str = None,
) -> str:
    """
    Get the assistant id from the email id or phone number.

    Args:
        email_id: The email id of the assistant.
        phone_number: The phone number of the assistant.

    Returns:
        The assistant id.
    """
    params = dict()
    if email_id:
        params["email"] = email_id
    if phone_number:
        params["phone"] = phone_number
    if "+15550100002" in phone_number:
        return "default-assistant", "cartesia", None
    if "+15550100001" in phone_number:
        return "default-assistant-2", "cartesia", None
    if "+15550100005" in phone_number:
        return "default-assistant-3", "cartesia", None
    response = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()
    if "detail" in response:
        return "default-assistant", "cartesia", None
    assistants = response["info"]
    if len(assistants) == 0:
        return "default-assistant", "cartesia", None
    return (
        assistants[0]["agent_id"],
        "cartesia",#assistants[0]["tts_provider"],
        assistants[0]["voice_id"],
    )


def start_service_if_not_running(assistant_id: str):
    """
    Start the service if it is not running.
    """
    if assistant_id not in ["default-assistant", "default-assistant-3"]:#"default-assistant-2",
        service_url = f"https://unity-{assistant_id}-000000000000.us-central1.run.app"
        headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
        response = requests.get(f"{service_url}/status", headers=headers).json()
        if not response["running"]:
            response = requests.post(f"{service_url}/start", headers=headers)
            if response.status_code != 200:
                print(f"Failed to start service for assistant {assistant_id}")


@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    print("twilio_whatsapp_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_id, _, _ = get_assistant_and_voice_info(phone_number=to_number)

    # start service if not running
    start_service_if_not_running(assistant_id)

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(
        os.getenv("PROJECT_ID"), f"unity-{assistant_id}"
    )
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "whatsapp",
                    "event": {
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        # Optionally, you might want to return an error response here
        # or modify resp_user to indicate failure.
        # For now, we'll just log the error and continue.
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")
