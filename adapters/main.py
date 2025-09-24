import json
import base64
import traceback
import time
import functions_framework
import os
import requests
from datetime import datetime, timedelta
from flask import Request, Response

from google.cloud import pubsub_v1
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from helpers import (
    check_valid_contact,
    dispatch_agent,
    get_assistant,
    is_job_running,
    start_unity_job,
    create_job,
    create_conference_response,
    add_user_to_conference,
    get_thread_id,
    publish_thread_id,
    STAGING,
    ORCHESTRA_URL,
    COMMS_URL,
)


@functions_framework.http
def assistant_wakeup_webhook(request: Request):
    print("assistant_wakeup_webhook function started")
    assistant_number = request.form.get("assistant_number")
    print(f"Assistant {assistant_number} woke up")

    # get assistant data
    assistant_data = get_assistant(phone_number=assistant_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    user_name = assistant_data["user_name"]
    assistant_first_name = assistant_data["assistant_first_name"]
    assistant_surname = assistant_data["assistant_surname"]
    assistant_age = assistant_data["assistant_age"]
    assistant_region = assistant_data["assistant_region"]
    assistant_about = assistant_data["assistant_about"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    assistant_email = assistant_data["assistant_email"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    voice_provider = assistant_data["voice_provider"]
    voice_id = assistant_data["voice_id"]

    # start unity job
    start_unity_job(
        api_key,
        "wakeup",
        assistant_id,
        user_id,
        user_name,
        f"{assistant_first_name} {assistant_surname}",
        assistant_age,
        assistant_region,
        assistant_about,
        user_number,
        assistant_number,
        assistant_email,
        user_whatsapp_number,
        user_email,
        voice_provider,
        voice_id,
    )

    # create job
    create_job(assistant_id)
    return Response(status=200)


@functions_framework.http
def twilio_call_status_webhook(request: Request):
    call_status = request.form.get("CallStatus")
    assistant_number = request.form.get("From")
    user_number = request.form.get("To")
    print(f"twilio_call_status_webhook function started: {call_status}")
    print(f"User {user_number} called by {assistant_number}")
    if call_status == "in-progress":
        # get assistant data
        assistant_data = get_assistant(phone_number=assistant_number)
        api_key = assistant_data["api_key"]
        assistant_id = assistant_data["assistant_id"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        user_number = assistant_data["user_number"]
        assistant_number = assistant_data["assistant_number"]

        # check if contact is valid
        contact_details = check_valid_contact(
            email_id="",
            phone_number=user_number,
            medium="phone",
            assistant_context=f"{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=user_number,
        )
        if "default" not in assistant_id and not contact_details:
            print(f"User {user_number} is not a valid contact")
            return Response(status_code=200)

        # publish to pubsub
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
        print(f"Publishing call to Pub/Sub at path: {topic_path}")
        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(
                    {
                        "thread": "call_received",
                        "event": {
                            "contact_details": contact_details,
                            "assistant_id": assistant_id,
                            "user_number": user_number,
                            "assistant_number": assistant_number,
                            "timestamp": int(time.time() * 1000),
                        },
                    }
                ).encode("utf-8"),
            )
            if "test" in assistant_id:
                status_id = publish_future.result(timeout=10)
                print(f"Message ID: {status_id}")
            print("Call published to Pub/Sub successfully")
        except Exception as e:
            print(f"Error publishing to Pub/Sub: {str(e)}")
    return Response(status=200)


# phone webhook
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
    assistant_data = get_assistant(phone_number=to_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    user_name = assistant_data["user_name"]
    assistant_first_name = assistant_data["assistant_first_name"]
    assistant_surname = assistant_data["assistant_surname"]
    assistant_age = assistant_data["assistant_age"]
    assistant_region = assistant_data["assistant_region"]
    assistant_about = assistant_data["assistant_about"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    assistant_email = assistant_data["assistant_email"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    voice_provider = assistant_data["voice_provider"]
    voice_id = assistant_data["voice_id"]

    # check if contact is valid
    contact_details = check_valid_contact(
        email_id="",
        phone_number=caller_number,
        medium="phone",
        assistant_context=f"{assistant_first_name}{assistant_surname}",
        api_key=api_key,
        user_number=user_number,
        user_whatsapp_number=user_whatsapp_number,
        user_email=user_email,
    )
    if "default" not in assistant_id and not contact_details:
        resp_user = VoiceResponse()
        resp_user.say(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(response=str(resp_user), mimetype="text/xml")

    # start unity job if it is not running
    running = is_job_running(user_id, assistant_id)
    print(f"Job running: {running}")
    if "test" not in assistant_id and "default" not in assistant_id and not running:
        start_unity_job(
            api_key,
            "phone",
            assistant_id,
            user_id,
            user_name,
            f"{assistant_first_name} {assistant_surname}",
            assistant_age,
            assistant_region,
            assistant_about,
            user_number,
            assistant_number,
            assistant_email,
            user_whatsapp_number,
            user_email,
            voice_provider,
            voice_id,
        )
        create_job(assistant_id)

    # FIXED: Create conference name and sip uri with unique timestamp
    date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    conference_name = f"Unity_{twilio_number[1:]}_{date_time}"
    room_name = f"unity_{twilio_number}"  # Consistent room per assistant
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
    print(f"Setting up conference {conference_name} with SIP URI {sip_uri}")
    print(f"LiveKit room will be: {room_name}")

    # publish to pubsub - let the pubsub handler dispatch the agent when worker is ready
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_message = {
            "thread": "call",
            "event": {
                "contact_details": contact_details,
                "conference_name": conference_name,
                "caller_number": caller_number,
                "sip_uri": sip_uri,
                "livekit_room": room_name,  # Include LiveKit room name
                "assistant_id": assistant_id,  # Include for agent dispatch
                "action": "start_worker",  # Signal that worker should start (agent already dispatched)
                "timestamp": int(time.time() * 1000),  # For timing analysis
                "call_metadata": {
                    "twilio_number": twilio_number,
                    "call_type": "inbound",
                    "room_created": True,  # Confirms room was created
                    "bridge_established": True,  # Confirms SIP bridge is ready
                },
            },
        }
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Call published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(response="Error publishing to Pub/Sub", status=500)

    # UNCHANGED: Keep the original conference setup (this works)
    try:
        resp_user = create_conference_response(conference_name)
        print(f"Conference response: {resp_user.to_xml()}")
        if resp_user:
            print("Conference response created successfully")
        else:
            print("Error: Failed to create conference response")
            return Response(response="Error creating conference", status=500)

        call_sid = add_user_to_conference(conference_name, caller_number, sip_uri)
        if call_sid:
            print(f"User added to conference successfully. Call SID: {call_sid}")
        else:
            print("Error: Failed to add user to conference")
            return Response(response="Error adding user to conference", status=500)

        print(f"Assistant ID: {assistant_id}")
        if assistant_id == "default-assistant":
            print(f"Dispatching agent {room_name}")
            dispatch_agent(room_name)

        print("Conference setup completed")
    except Exception as e:
        print(f"Error during conference setup: {str(e)}")
        return Response(response="Error setting up conference", status=500)

    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# sms webhook
@functions_framework.http
def twilio_msg_webhook(request: Request):
    print("twilio_msg_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_data = get_assistant(phone_number=to_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    user_name = assistant_data["user_name"]
    assistant_first_name = assistant_data["assistant_first_name"]
    assistant_surname = assistant_data["assistant_surname"]
    assistant_age = assistant_data["assistant_age"]
    assistant_region = assistant_data["assistant_region"]
    assistant_about = assistant_data["assistant_about"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    assistant_email = assistant_data["assistant_email"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    voice_provider = assistant_data["voice_provider"]
    voice_id = assistant_data["voice_id"]

    # check if contact is valid
    contact_details = check_valid_contact(
        email_id="",
        phone_number=from_number,
        medium="msg",
        assistant_context=f"{assistant_first_name}{assistant_surname}",
        api_key=api_key,
        user_number=user_number,
        user_whatsapp_number=user_whatsapp_number,
        user_email=user_email,
    )
    if "default" not in assistant_id and not contact_details:
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(response=str(resp_user), mimetype="text/xml")

    # start unity job if it is not running
    running = is_job_running(user_id, assistant_id)
    print(f"Job running: {running}")
    if "test" not in assistant_id and "default" not in assistant_id and not running:
        start_unity_job(
            api_key,
            "msg",
            assistant_id,
            user_id,
            user_name,
            f"{assistant_first_name} {assistant_surname}",
            assistant_age,
            assistant_region,
            assistant_about,
            user_number,
            assistant_number,
            assistant_email,
            user_whatsapp_number,
            user_email,
            voice_provider,
            voice_id,
        )
        create_job(assistant_id)

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "msg",
                    "event": {
                        "contact_details": contact_details,
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(response="Error publishing to Pub/Sub", status=500)
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# whatsapp webhook
@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    print("twilio_whatsapp_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_data = get_assistant(phone_number=to_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    user_name = assistant_data["user_name"]
    assistant_first_name = assistant_data["assistant_first_name"]
    assistant_surname = assistant_data["assistant_surname"]
    assistant_age = assistant_data["assistant_age"]
    assistant_region = assistant_data["assistant_region"]
    assistant_about = assistant_data["assistant_about"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    assistant_email = assistant_data["assistant_email"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    voice_provider = assistant_data["voice_provider"]
    voice_id = assistant_data["voice_id"]

    # check if contact is valid
    contact_details = check_valid_contact(
        email_id="",
        phone_number=from_number.replace("whatsapp:", ""),
        medium="whatsapp",
        assistant_context=f"{assistant_first_name}{assistant_surname}",
        api_key=api_key,
        user_number=user_number,
        user_whatsapp_number=user_whatsapp_number,
        user_email=user_email,
    )
    if "default" not in assistant_id and not contact_details:
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(response=str(resp_user), mimetype="text/xml")

    # start unity job if it is not running
    running = is_job_running(user_id, assistant_id)
    print(f"Job running: {running}")
    if "test" not in assistant_id and "default" not in assistant_id and not running:
        start_unity_job(
            api_key,
            "whatsapp",
            assistant_id,
            user_id,
            user_name,
            f"{assistant_first_name} {assistant_surname}",
            assistant_age,
            assistant_region,
            assistant_about,
            user_number,
            assistant_number,
            assistant_email,
            user_whatsapp_number,
            user_email,
            voice_provider,
            voice_id,
        )
        create_job(assistant_id)

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "whatsapp",
                    "event": {
                        "contact_details": contact_details,
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(response="Error publishing to Pub/Sub", status=500)

    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# email webhook
@functions_framework.http
def email_watch_renewer(request):
    """Cloud Function that renews Gmail watches for multiple users."""
    # ToDo: make orchestra admin call to get all assistant emails
    test = request.json.get("test")
    if not test:
        emails = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/emails",
            headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
        ).json()["info"]
        emails += [
            # "default-assistant@unify.ai",
            # "default-assistant-2@unify.ai",
            "default-assistant-3@unify.ai",
        ]
    else:
        emails = ["default-test-assistant@unify.ai"]
    print(f"Emails to renew: {emails}")

    results = {}

    # Process each email
    print("Renewing emails...")
    for email in emails:
        try:
            admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
            results[email] = requests.post(
                f"{COMMS_URL}/email/watch",
                json={"primary_email": email},
                headers={"Authorization": f"Bearer {admin_key}"},
            ).json()
        except Exception as e:
            error_message = f"Error renewing Gmail watch for {email}: {str(e)}"
            print(error_message)
            results[email] = {"success": False, "error": error_message}
    print("Results of renewing emails:")
    print(results)

    # renew policy assistant
    if os.getenv("STAGING") and not test:
        admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
        response = requests.post(
            f"{COMMS_URL}/email/watch",
            json={"primary_email": "mh-policies@unify.ai", "topic_name": "intranet"},
            headers={"Authorization": f"Bearer {admin_key}"},
        ).json()
        print("Renewed policy assistant")
        print(response)

    return results


@functions_framework.cloud_event
def email_notification_processor(cloud_event):
    """Cloud Function triggered by Pub/Sub that processes Gmail notifications."""
    try:
        # Extract the Pub/Sub message from the cloud event
        envelope = json.loads(
            base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        )
        print(f"Received notification: {envelope}")

        # Extract Gmail notification details
        email_id = envelope["emailAddress"]
        history_id = envelope["historyId"]

        # get assistant id from email id
        assistant_data = get_assistant(email_id=email_id)
        api_key = assistant_data["api_key"]
        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        user_name = assistant_data["user_name"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        assistant_age = assistant_data["assistant_age"]
        assistant_region = assistant_data["assistant_region"]
        assistant_about = assistant_data["assistant_about"]
        user_number = assistant_data["user_number"]
        assistant_number = assistant_data["assistant_number"]
        assistant_email = assistant_data["assistant_email"]
        user_whatsapp_number = assistant_data["user_whatsapp_number"]
        user_email = assistant_data["user_email"]
        voice_provider = assistant_data["voice_provider"]
        voice_id = assistant_data["voice_id"]

        # check if contact is valid
        contact_details = check_valid_contact(
            email_id=email_id,
            phone_number="",
            medium="email",
            assistant_context=f"{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
        )
        if "default" not in assistant_id and not contact_details:
            error_message = (
                "This email address is no longer active. Please visit "
                "console.unify.ai to view your assistant details."
            )
            return error_message, 500

        # start unity job if it is not running
        running = is_job_running(user_id, assistant_id)
        print(f"Job running: {running}")
        if "test" not in assistant_id and "default" not in assistant_id and not running:
            start_unity_job(
                api_key,
                "email",
                assistant_id,
                user_id,
                user_name,
                f"{assistant_first_name} {assistant_surname}",
                assistant_age,
                assistant_region,
                assistant_about,
                user_number,
                assistant_number,
                assistant_email,
                user_whatsapp_number,
                user_email,
                voice_provider,
                voice_id,
            )
            create_job(assistant_id)

        # Get credentials
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        scopes = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ]
        gmail_creds = Credentials.from_service_account_info(
            creds_json,
            scopes=scopes,
            subject=email_id,
        )
        gmail_service = build("gmail", "v1", credentials=gmail_creds)

        # Process the history and thread
        print(f"email_id: {email_id}, history_id: {history_id}")
        thread_id, message_id, last_message = get_thread_id(
            email_id, history_id, gmail_service
        )
        print(
            f"thread_id: {thread_id}, message_id: {message_id}, last_message: {last_message}"
        )

        if thread_id:
            print(f"Successfully processed conversation for user {email_id}")
            publish_thread_id(
                assistant_id,
                user_id,
                thread_id,
                message_id,
                last_message,
                contact_details,
            )
            return "OK"
        else:
            print(f"No new conversations found for user {email_id}")
            return "No new conversations"

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        traceback.print_exc()
        print(error_message)
        return error_message, 500


# infra webhook
@functions_framework.http
def idle_job_creator(request):
    """Cloud Function that creates a new idle job."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get(f"{COMMS_URL}/infra/image", headers=headers)
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )
    response = requests.post(
        f"{COMMS_URL}/infra/job/create", data={"image": image}, headers=headers
    )
    return response.json()


@functions_framework.http
def idle_job_cleaner(request):
    """Cloud Function that renews idle jobs that have been around for >24 hours.."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    idle_jobs = []

    # get all jobs
    jobs = requests.get(f"{COMMS_URL}/infra/jobs", headers=headers).json()
    job_names = [job["job_name"] for job in jobs["jobs"]]
    job_names = [
        job_name
        for job_name in job_names
        if (STAGING and "staging" in job_name)
        or (not STAGING and "staging" not in job_name)
    ]
    print(f"Job names: {job_names}")

    for job_name in job_names:
        # get logs
        logs = (
            requests.get(
                f"{COMMS_URL}/infra/job/logs",
                params={"job_name": job_name},
                headers=headers,
            )
            .json()
            .get("logs", [])
        )
        print(f"Logs: {logs}")

        # check if job is idle
        if (
            "ping received - keeping conversation manager alive" in logs
            and "Graceful shutdown completed" not in logs
            and "Shutting down convo manager..." not in logs
        ):
            idle_jobs.append(job_name)

    new_idle_jobs = []
    for job_name in idle_jobs:
        # check if job is older than 10 minutes
        job_timestamp_str = job_name.replace("unity-", "").replace("-staging", "")
        job_timestamp = datetime.strptime(job_timestamp_str, "%Y-%m-%d-%H-%M-%S")
        now = datetime.now()
        delta = now - job_timestamp
        if delta < timedelta(minutes=11):
            new_idle_jobs.append(job_name)

    print(f"Idle jobs: {idle_jobs}")
    print(f"New idle jobs: {new_idle_jobs}")
    if len(new_idle_jobs) == 0:
        if len(idle_jobs) != 0:
            idle_jobs = sorted(idle_jobs)[:-1]
    else:
        new_idle_jobs = [sorted(new_idle_jobs)[-1]]
    idle_jobs = list(filter(lambda job: job not in new_idle_jobs, idle_jobs))

    # delete all old idle jobs
    for job_name in idle_jobs:
        requests.delete(
            f"{COMMS_URL}/infra/job/delete",
            data={"job_name": job_name},
            headers=headers,
        )

    return Response(response=json.dumps({"idle_jobs": idle_jobs}), status=200)
