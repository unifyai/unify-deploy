import os
import json
import logging
import random
import string
from fastapi import APIRouter, HTTPException, Request
from dotenv import load_dotenv
import httpx
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64

load_dotenv()

router = APIRouter()

# with open(os.environ["GCP_SA_KEY"], "r") as f:
creds_json = json.loads(os.environ["GCP_SA_KEY"])


# Helpers
def get_admin_service():
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/admin.directory.user"],
        subject="dan@unify.ai",
    )
    service = build("admin", "directory_v1", credentials=creds)
    return service


def get_gmail_service(sender_email: str):
    # include send and readonly scopes for reading history and replying
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    creds = Credentials.from_service_account_info(
        creds_json, scopes=scopes, subject=sender_email
    )
    return build("gmail", "v1", credentials=creds)


# Endpoints - JSON format
@router.post("/create", status_code=201)
async def create_email_user(request: Request):
    data = await request.json()
    local = data.get("local")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    if not local or not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: local, first_name, last_name",
        )
    domain = "unify.ai"
    primary_email = f"{local}@{domain}"
    # generate secure password
    password = "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(32)
    )
    try:
        service = get_admin_service()
        user_body = {
            "name": {"givenName": first_name, "familyName": last_name},
            "primaryEmail": primary_email,
            "password": password,
        }
        res = service.users().insert(body=user_body).execute()
        # optional watch call
        async with httpx.AsyncClient() as client_http:
            watch_res = await client_http.post(
                f"{os.getenv('UNITY_COMMS_URL')}/api/email/watch",
                json={"userEmail": primary_email},
            )
        return {"success": True, "user": res}
    except Exception as e:
        logging.error("Failed to create user: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete")
async def delete_email_user(request: Request):
    data = await request.json()
    primary_email = data.get("primary_email")
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")
    try:
        service = get_admin_service()
        service.users().delete(userKey=primary_email).execute()
        return {"success": True, "message": f"User {primary_email} deleted."}
    except Exception as e:
        logging.error("Failed to delete user: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send")
async def send_email(request: Request):
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    cc = data.get("cc")
    bcc = data.get("bcc")
    subject = data.get("subject", "")
    body = data.get("body")
    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400, detail="Missing required fields: 'from', 'to', 'body'"
        )
    msg = MIMEMultipart()
    msg["from"] = sender
    msg["to"] = to if isinstance(to, str) else ",".join(to)
    if cc:
        msg["cc"] = cc if isinstance(cc, str) else ",".join(cc)
    if bcc:
        msg["bcc"] = bcc if isinstance(bcc, str) else ",".join(bcc)
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = get_gmail_service(sender)
    sent = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()
    return {"success": True, "id": sent.get("id")}


@router.post("/watch")
async def watch_email(request: Request):
    data = await request.json()
    user_email = data.get("primary_email")
    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
        subject=user_email,
    )
    gmail_service = build("gmail", "v1", credentials=creds)
    topic_name = "projects/gcp-project-runtime/topics/" + data.get(
        "topic_name", "email-notifications"
    )
    watch_request = {"labelIds": ["INBOX"], "topicName": topic_name}
    watch_resp = (
        gmail_service.users()
        .watch(
            userId="me",
            body=watch_request,
        )
        .execute()
    )
    return {"success": True, "historyId": watch_resp.get("historyId")}
