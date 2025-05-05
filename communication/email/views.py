import os
import json
import logging
import random
import string
from fastapi import APIRouter, HTTPException, Request
from dotenv import load_dotenv
import httpx
from google.oauth2.service_account import Credentials
from google.auth import default
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64
import unify
load_dotenv()

router = APIRouter()
client = unify.Unify(traced=True)
client.set_endpoint("o4-mini@openai")
client.set_system_message("You are a helpful assistant.")

# with open(os.environ["GCP_SA_KEY"], "r") as f:
creds_json = json.loads(os.environ["GCP_SA_KEY"])

# Google Admin Directory API settings
def get_admin_service():
    creds = Credentials.from_service_account_info(
        creds_json, 
        scopes=["https://www.googleapis.com/auth/admin.directory.user"], 
        subject="dan@unify.ai"
    )
    service = build("admin", "directory_v1", credentials=creds)
    return service

# Gmail send endpoint
def get_gmail_service(sender_email: str):
    # include send and readonly scopes for reading history and replying
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    creds = Credentials.from_service_account_info(
        creds_json, 
        scopes=scopes,
        subject=sender_email
    )
    return build("gmail", "v1", credentials=creds)

@router.post("/create", status_code=201)
async def create_email_user(request: Request):
    data = await request.json()
    first_name = data.get("firstName")
    last_name = data.get("lastName")
    if not first_name or not last_name:
        raise HTTPException(status_code=400, detail="Missing required fields: firstName, lastName")
    domain = "unify.ai"
    local = f"{first_name}.{last_name}".lower()
    # sanitize local part
    local = ''.join(c for c in local if c.isalnum() or c == '.')
    primary_email = f"{local}@{domain}"
    # generate secure password
    password = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
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
                f"{os.getenv('UNIFY_COMMS_URL')}/api/email/watch",
                json={"userEmail": primary_email},
            )
        return {"success": True, "user": res}
    except Exception as e:
        logging.error("Failed to create user: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/delete")
async def delete_email_user(request: Request):
    data = await request.json()
    primary_email = data.get("primaryEmail")
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primaryEmail")
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
        raise HTTPException(status_code=400, detail="Missing required fields: 'from', 'to', 'body'")
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

@router.post("/reply")
async def reply_email(request: Request):
    envelope = await request.json()
    pub_message = envelope.get("message")
    if not pub_message or "data" not in pub_message:
        raise HTTPException(status_code=400, detail="Invalid Pub/Sub message")
    data_str = base64.urlsafe_b64decode(pub_message["data"]).decode()
    push_data = json.loads(data_str)
    email_address = push_data.get("emailAddress")
    if not email_address:
        raise HTTPException(status_code=400, detail="Missing emailAddress")
    service = get_gmail_service(email_address)

    # search for unread messages newer than 1 day
    query = "is:unread newer_than:1d"
    res = service.users().messages().list(userId="me", q=query).execute()
    msgs = res.get("messages", [])
    logging.warning(f"Full response obtained: {len(msgs)}")
    if not msgs:
        return {"success": False, "error": "No unread messages."}
    msg_id = msgs[-1]["id"]
    # get full message
    orig = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    thread_id = orig.get("threadId")
    headers = orig.get("payload", {}).get("headers", [])
    frm = next((h["value"] for h in headers if h.get("name")=="From"), None)
    to = next((h["value"] for h in headers if h.get("name")=="To"), None)
    subj = next((h["value"] for h in headers if h.get("name")=="Subject"), "")
    # decode message body
    body = ""
    payload = orig.get("payload", {})
    if payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType")=="text/plain":
                data_b = part.get("body", {}).get("data", "")
                if data_b:
                    body = base64.urlsafe_b64decode(data_b).decode()
                    break
    else:
        data_b = payload.get("body", {}).get("data", "")
        if data_b:
            body = base64.urlsafe_b64decode(data_b).decode()

    # mark as read
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds":["UNREAD"]}
    ).execute()

    # generate reply
    messages = [
        {"role": "user", "content": body},
    ]
    # Call Unify ChatCompletion
    response = client.generate(
        messages=messages,
    )

    # build threaded reply
    reply_subj = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    mime = MIMEMultipart()
    mime["to"] = frm
    mime["from"] = email_address
    mime["subject"] = reply_subj
    orig_msg_id = next((h["value"] for h in headers if h.get("name")=="Message-ID"), None)
    if orig_msg_id:
        mime["In-Reply-To"] = orig_msg_id
        mime["References"] = orig_msg_id
    reply_body = f"Automated reply:\n{response}"
    mime.attach(MIMEText(reply_body, "plain"))
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    return {"success": True, "replyId": sent.get("id")}

@router.post("/watch")
async def watch_email(request: Request):
    data = await request.json()
    user_email = data.get("userEmail")
    if not user_email:
        raise HTTPException(status_code=400, detail="Missing userEmail")
    # Delegate credentials for the target Gmail user
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
        subject=user_email,
    )
    gmail_service = build("gmail", "v1", credentials=creds)
    topic_name = "projects/gcp-project-runtime/topics/email-notifications"
    watch_request = {
        "labelIds": ["INBOX"],
        "topicName": topic_name
    }
    watch_resp = gmail_service.users().watch(
        userId="me",
        body=watch_request,
    ).execute()
    return {"success": True, "historyId": watch_resp.get("historyId")}
