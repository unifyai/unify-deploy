import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
from twilio.twiml.messaging_response import MessagingResponse
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)


@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    logging.info("twilio_whatsapp_webhook function started.")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    logging.info(f"Received message from {from_number} to {to_number} with body: {body}")

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    logging.info("Publishing message to Pub/Sub.")
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), "whatsapp")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "to_number": to_number,
                    "from_number": from_number,
                    "body": body,
                }
            ).encode("utf-8"),
        )
        logging.info("Message published to Pub/Sub successfully.")
    except Exception as e:
        logging.error(f"Error publishing to Pub/Sub: {e}")
        # Optionally, you might want to return an error response here
        # or modify resp_user to indicate failure.
        # For now, we'll just log the error and continue.
    logging.info("Returning TwiML response.")
    return Response(response=str(resp_user), mimetype="text/xml")
