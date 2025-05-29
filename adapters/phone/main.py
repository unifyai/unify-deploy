# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
import requests
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse
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
        return "default_assistant"
    assistants = response["info"]
    if len(assistants) == 0:
        return "default_assistant"
    return assistants[0]["agent_id"]


def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


def create_conference_response(conference_name, with_status=False):
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    if with_status:
        dial_user.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True,
            muted=False,
            record="record-from-start",
            recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
            recording_status_callback_event="completed",
            status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/call-status",
            status_callback_event=["completed"],
        )
        return resp_user
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        record="record-from-start",
        recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
        recording_status_callback_event="completed",
    )
    return resp_user


def add_user_to_conference(
    conference_name, from_number, to_number_uri, connect_third_party=False
):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(
            friendly_name=conference_name, status="in-progress"
        )
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(
                    participant.sid
                ).update(muted=True)
                break
        response = create_conference_response(conference_name, with_status=True)
    else:
        response = create_conference_response(conference_name)

    call = twilio_client.calls.create(
        to=to_number_uri,
        from_=from_number,
        twiml=str(response),
    )
    return call.sid


@functions_framework.http
def twilio_call_webhook(request: Request):
    print("twilio_call_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""
    print(f"Received call from {caller_number} to {twilio_number}")

    # get assistant id from email id
    assistant_id = get_assistant_id(phone_number=to_number)

    # get conference name and sip uri
    conference_name = f"Unity_{twilio_number[1:]}"
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
    print(f"Setting up conference {conference_name} with SIP URI {sip_uri}")

    # set up conference
    resp_user = create_conference_response(conference_name)
    add_user_to_conference(conference_name, caller_number, sip_uri)
    print("Conference setup completed")

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), assistant_id)
    print(f"Publishing call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "call",
                    "event": {
                        "conference_name": conference_name,
                        "caller_number": caller_number,
                        "sip_uri": sip_uri,
                    },
                }
            ).encode("utf-8"),
        )
        print("Call published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


@functions_framework.http
def twilio_msg_webhook(request: Request):
    print("twilio_msg_webhook function started")
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
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), assistant_id)
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "msg",
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
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")
