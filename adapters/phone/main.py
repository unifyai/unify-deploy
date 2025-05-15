# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

import json
from flask import Request
import functions_framework
from google.cloud import pubsub_v1
import os
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse


# def get_twilio_client():
#     account_sid = os.getenv("TWILIO_ACCOUNT_SID")
#     auth_token = os.getenv("TWILIO_AUTH_TOKEN")
#     if not account_sid or not auth_token:
#         raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
#     return TwilioClient(account_sid, auth_token)


# def create_conference_response(conference_name, with_status=False):
#     resp_user = VoiceResponse()
#     dial_user = resp_user.dial()
#     if with_status:
#         dial_user.conference(
#             conference_name,
#             startConferenceOnEnter=True,
#             endConferenceOnExit=True,
#             muted=False,
#             record="record-from-start",
#             recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
#             recording_status_callback_event='completed',
#             status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/call-status",
#             status_callback_event=["completed"]
#         )
#         return resp_user
#     dial_user.conference(
#         conference_name,
#         startConferenceOnEnter=True,
#         endConferenceOnExit=True,
#         muted=False,
#         record="record-from-start",
#         recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
#         recording_status_callback_event='completed'
#     )
#     return resp_user


# def add_user_to_conference(conference_name, from_number, to_number_uri, connect_third_party=False):
#     twilio_client = get_twilio_client()

#     if connect_third_party:
#         conferences = twilio_client.conferences.list(friendly_name=conference_name, status="in-progress")
#         participants = twilio_client.conferences(conferences[0].sid).participants.list()
#         for participant in participants:
#             call = twilio_client.calls(participant.call_sid).fetch()
#             # Identify Livekit Agent and mute
#             if "livekit.cloud" in call.to:
#                 twilio_client.conferences(conferences[0].sid).participants(participant.sid).update(muted=True)
#                 break 
#         response = create_conference_response(conference_name, with_status=True)
#     else:
#         response = create_conference_response(conference_name)

#     call = twilio_client.calls.create(
#         to=to_number_uri,
#         from_=from_number, 
#         twiml=str(response),
#     )
#     return call.sid


@functions_framework.http
def twilio_webhook(request: Request):
    to_number   = request.form.get("To", "")
    from_number = request.form.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""

    conference_name = f"Unity_{twilio_number[1:]}"
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"

    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), "phone")
    pubsub_client.publish(topic_path, json.dumps({
        "conference_name": conference_name,
        "caller_number": caller_number,
        "sip_uri": sip_uri
    }).encode("utf-8"))
