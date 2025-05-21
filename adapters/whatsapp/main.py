import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
from twilio.twiml.messaging_response import MessagingResponse


@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), "whatsapp")
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
    return Response(response=str(resp_user), mimetype="text/xml")
