import json
import base64
import functions_framework
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import os
import requests

from .helpers import (
    get_assistant,
    get_thread_id,
    publish_thread_id,
    start_unity_job,
    ORCHESTRA_URL,
    COMMS_URL,
)


@functions_framework.http
def renew_watch(request):
    """Cloud Function that renews Gmail watches for multiple users."""
    # ToDo: make orchestra admin call to get all assistant emails
    emails = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant/emails",
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()["info"]
    emails += [
        # "default-assistant@unify.ai",
        # "default-assistant-2@unify.ai",
        "default-assistant-3@unify.ai",
    ]

    results = {}

    # Process each email
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

    return results


@functions_framework.cloud_event
def process_notification(cloud_event):
    """Cloud Function triggered by Pub/Sub that processes Gmail notifications."""
    try:
        # Extract the Pub/Sub message from the cloud event
        envelope = json.loads(
            base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        )
        print(f"Received notification: {envelope}")

        # Extract Gmail notification details
        user_id = envelope["emailAddress"]
        history_id = envelope["historyId"]

        # get assistant id from email id
        assistant_data = get_assistant(email_id=user_id)
        api_key = assistant_data["api_key"]
        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        user_name = assistant_data["user_name"]
        user_email = assistant_data["user_email"]
        assistant_name = assistant_data["assistant_name"]
        assistant_age = assistant_data["assistant_age"]
        assistant_region = assistant_data["assistant_region"]
        assistant_about = assistant_data["assistant_about"]
        user_number = assistant_data["user_number"]
        assistant_number = assistant_data["assistant_number"]
        assistant_email = assistant_data["assistant_email"]
        # user_phone_number = assistant_data["user_phone_number"]

        # start unity job
        start_unity_job(
            api_key,
            "email",
            assistant_id,
            user_id,
            user_name,
            assistant_name,
            assistant_age,
            assistant_region,
            assistant_about,
            user_number,
            assistant_number,
            assistant_email,
            user_number,  # user_phone_number,
            user_email,
        )

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
            subject=user_id,
        )
        gmail_service = build("gmail", "v1", credentials=gmail_creds)

        # Process the history and thread
        thread_id, last_message = get_thread_id(user_id, history_id, gmail_service)

        if thread_id:
            print(f"Successfully processed conversation for user {user_id}")
            publish_thread_id(assistant_id, thread_id, user_id, last_message)
            return "OK"
        else:
            print(f"No new conversations found for user {user_id}")
            return "No new conversations"

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        print(error_message)
        return error_message, 500
