import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
from twilio.twiml.messaging_response import MessagingResponse

from adapters.helpers import get_assistant_id

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
            json.dumps({
                "thread": "whatsapp",
                "event": {
                    "to_number": to_number,
                    "from_number": from_number,
                    "body": body,
                },
            }).encode("utf-8"),
        )
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        # Optionally, you might want to return an error response here
        # or modify resp_user to indicate failure.
        # For now, we'll just log the error and continue.
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")
