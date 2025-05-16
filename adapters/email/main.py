import json
import base64
import functions_framework
from google.auth import default
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from helpers import get_thread_id, publish_thread_id
import os
import requests


@functions_framework.http
def renew_watch():
    """Cloud Function that renews Gmail watches for multiple users."""
    # ToDo: make orchestra admin call to get all assistant emails
    emails = requests.get(
        "https://api.unify.ai/v0/admin/assistant/emails",
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()["info"]

    results = {}

    # Process each email
    for email in emails:
        try:
            # Get credentials from Application Default Credentials
            credentials, _ = default()

            # Configure for this specific email
            credentials = credentials.with_subject(email)
            credentials = credentials.with_scopes(
                ["https://www.googleapis.com/auth/gmail.modify"]
            )
            credentials.refresh(Request())

            # Build Gmail service for this email
            gmail_service = build("gmail", "v1", credentials=credentials)

            # Configure watch request
            watch_request = {
                "labelIds": ["INBOX"],
                "topicName": "projects/gcp-project-runtime/topics/email-notifications-test",
            }

            # Execute watch request
            watch_resp = (
                gmail_service.users()
                .watch(
                    userId="me",
                    body=watch_request,
                )
                .execute()
            )

            results[email] = {"success": True, "response": watch_resp}

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

        # Get credentials for this specific email
        credentials, _ = default()
        credentials = credentials.with_subject(user_id)
        credentials = credentials.with_scopes(
            [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ]
        )
        credentials.refresh(Request())

        # Build Gmail service for this specific user
        gmail_service = build("gmail", "v1", credentials=credentials)

        # Process the history and thread
        thread_id = get_thread_id(user_id, history_id, gmail_service)

        # Here you would add your business logic to do something with the conversation
        # For example, send it to another service, store it in a database, etc.

        if thread_id:
            # ToDo: check if the conversation stored for this thread_id has changed
            # send the thread_id to a different channel
            print(f"Successfully processed conversation for user {user_id}")
            publish_thread_id(thread_id, user_id)
            return "OK"
        else:
            print(f"No new conversations found for user {user_id}")
            return "No new conversations"

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        print(error_message)
        return error_message, 500
