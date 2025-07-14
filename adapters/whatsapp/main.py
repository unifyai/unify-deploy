import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
import requests
from twilio.twiml.messaging_response import MessagingResponse


STAGING = os.getenv("STAGING")
ORCHESTRA_URL = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://service.a.run.app/v0"
)
COMMS_URL = (
    "https://unity-comms-app-000000000000.us-central1.run.app"
    if not STAGING
    else "https://unity-comms-app-staging-000000000000.us-central1.run.app"
)


def get_assistant(email_id: str = None, phone_number: str = None) -> dict[str, str]:
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

    default_assistant_data = {
        "assistant_id": "default-assistant",
        "tts_provider": "cartesia",
        "voice_id": None,
        "api_key": "",
        "user_name": "",
        "assistant_name": "Default Assistant",
        "user_number": "",
        "assistant_number": "",
        # "user_phone_number": "",
    }
    if "+15550100002" in phone_number:
        return default_assistant_data
    if "+15550100001" in phone_number:
        return {**default_assistant_data, "assistant_id": "default-assistant-2"}
    if "+15550100005" in phone_number:
        return {
            **default_assistant_data,
            "api_key": "",
            "assistant_id": "default-assistant-3",
            "user_name": "Ved",
            "user_number": "+15550100004",
            "assistant_number": "+15550100005",
            # "user_phone_number": "+15550100004",
        }
    if "+15550100007" in phone_number:
        return {**default_assistant_data, "assistant_id": "default-assistant-4"}

    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()

    if "detail" in response:
        return default_assistant_data
    assistants = response["info"]
    if len(assistants) == 0:
        return default_assistant_data

    return {
        "assistant_id": assistants[0]["agent_id"],
        "api_key": assistants[0]["api_key"],
        "user_name": f"{assistants[0]['user_first_name']} {assistants[0]['user_last_name']}",
        "assistant_name": f"{assistants[0]['first_name']} {assistants[0]['surname']}",
        "assistant_number": assistants[0]["phone"],
        "assistant_whatsapp_number": assistants[0]["assistant_whatsapp_number"],
        "assistant_email": assistants[0]["email"],
        "user_number": assistants[0]["user_phone"],
        "user_whatsapp_number": assistants[0]["user_whatsapp_number"],
        "tts_provider": assistants[0]["tts_provider"],
        "voice_id": assistants[0]["voice_id"],
    }


def start_unity_job(
    api_key: str,
    assistant_id: str,
    user_name: str,
    assistant_name: str,
    user_number: str,
    assistant_number: str,
    user_phone_number: str,
):
    """
    Start the service if it is not running.

    Args:
        api_key: The API key for the assistant.
        assistant_id: The ID of the assistant.
        user_name: The name of the user.
        assistant_name: The name of the assistant.
        user_number: The phone number of the user.
        assistant_number: The phone number of the assistant.
        user_phone_number: The phone number of the user.
    """
    # default option when api key isn't set
    if user_name == "":
        print(f"No user name for assistant {assistant_id}")
        return

    # get commit hash
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get(
        f"{COMMS_URL}/infra/image",
        headers=headers,
    )
    if response.status_code != 200:
        print(f"Failed to get commit hash for assistant {assistant_id}")
        return
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )

    # create job
    response = requests.post(
        f"{COMMS_URL}/infra/job/create",
        headers=headers,
        data={
            "api_key": api_key,
            "assistant_id": assistant_id,
            "user_name": user_name,
            "assistant_name": assistant_name,
            "user_number": user_number,
            "assistant_number": assistant_number,
            "user_phone_number": user_phone_number,
            "image": image,
        },
    )
    if response.status_code != 200:
        print(f"Failed to create job for assistant {assistant_id}")


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
    user_name = assistant_data["user_name"]
    assistant_name = assistant_data["assistant_name"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    # user_phone_number = assistant_data["user_phone_number"]

    # cold message is only for user to their own assistant
    if not assistant_id:
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit console.unify.ai to view your assistant details."
        )
        return Response(response=str(resp_user), mimetype="text/xml")

    # start unity job
    start_unity_job(
        api_key,
        assistant_id,
        user_name,
        assistant_name,
        user_number,
        assistant_number,
        user_number,  # user_phone_number,
    )

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
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
