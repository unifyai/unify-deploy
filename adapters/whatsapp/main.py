import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
from twilio.twiml.messaging_response import MessagingResponse
import logging

# Get the logger for this module
logger = logging.getLogger(__name__)

@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    logger.info("twilio_whatsapp_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    logger.info("Received WhatsApp message", extra={
        "from_number": from_number,
        "to_number": to_number,
        "body": body
    })

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    logger.info("Publishing message to Pub/Sub")
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
        logger.info("Message published to Pub/Sub successfully")
    except Exception as e:
        logger.error("Error publishing to Pub/Sub", exc_info=True)
        # Optionally, you might want to return an error response here
        # or modify resp_user to indicate failure.
        # For now, we'll just log the error and continue.
    logger.info("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")
