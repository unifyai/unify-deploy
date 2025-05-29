import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
import requests
from twilio.twiml.messaging_response import MessagingResponse


def get_assistant_id(
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
        params["phone_number"] = phone_number
    response = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()
    if "detail" in response:
        return "default-assistant"
    assistants = response["info"]
    if len(assistants) == 0:
        return "default-assistant"
    return assistants[0]["agent_id"]


@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    print("twilio_whatsapp_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_id = get_assistant_id(phone_number=to_number)

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    print("Publishing message to Pub/Sub")
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), assistant_id)
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
