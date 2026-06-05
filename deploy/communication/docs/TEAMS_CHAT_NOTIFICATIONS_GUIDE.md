# Microsoft Teams Chat Notifications Setup Guide

This guide walks you through setting up real-time notifications for incoming Microsoft Teams chat messages using Microsoft Graph API webhooks (change notifications).

## Quick Start (Unify Platform)

If you're using the Unify platform, Teams chat is integrated and works automatically once OAuth is complete:

### Prerequisites
1. Complete Microsoft OAuth setup (see [MICROSOFT_OAUTH_SETUP_GUIDE.md](./MICROSOFT_OAUTH_SETUP_GUIDE.md))
2. Ensure `Chat.Read` and `Chat.ReadWrite` permissions are granted

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /teams/send` | Send a message to a Teams chat |
| `POST /teams/watch` | Create subscription for chat notifications |
| `DELETE /teams/watch` | Delete chat subscription |
| `GET /teams/chats` | List all chats for a user |
| `GET /teams/messages/{chat_id}` | Get messages from a chat |

### Subscription Lifecycle
- Teams chat subscriptions expire in **60 minutes** (Microsoft limit)
- A Cloud Scheduler job runs **every 30 minutes** to renew subscriptions (aligns with token refresh)
- Notifications are routed through `/microsoft/router` → `/chat/teams`

### Sending a Chat Message
```bash
curl -X POST https://communication.unify.ai/teams/send \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "assistant@company.com",
    "chat_id": "19:meeting_xxx@thread.v2",
    "body": "Hello from Unify!"
  }'
```

---

## Table of Contents

1. [Overview](#overview)
   - [How It Works](#how-it-works)
   - [Architecture](#architecture)
   - [Notification Types](#notification-types)
2. [Prerequisites](#prerequisites)
3. [Step 1: Register Azure AD Application](#step-1-register-azure-ad-application)
4. [Step 2: Configure API Permissions (Delegated)](#step-2-configure-api-permissions)
5. [Step 3: Authorize the User (OAuth Flow)](#step-3-authorize-the-user-oauth-flow)
6. [Step 4: Set Up Webhook Endpoint](#step-4-set-up-webhook-endpoint)
7. [Step 5: Create Change Notification Subscription](#step-5-create-change-notification-subscription)
8. [Step 6: Handle Incoming Notifications](#step-6-handle-incoming-notifications)
9. [Step 7: Renew Subscriptions](#step-7-renew-subscriptions)
10. [Step 8: Sending Replies](#step-8-sending-replies-optional)
11. [Troubleshooting](#troubleshooting)
12. [Code Examples](#code-examples)

---

## Overview

### How It Works

Microsoft Graph API provides **change notifications** (webhooks) that push real-time updates when events occur in Microsoft 365 services, including Teams chat messages.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    TEAMS CHAT NOTIFICATION FLOW                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  1. User sends message in Teams chat                               │
│         │                                                           │
│         ▼                                                           │
│  2. Microsoft Graph detects change                                 │
│         │                                                           │
│         ▼                                                           │
│  3. Graph sends POST to your webhook URL                           │
│     https://your-app.com/webhooks/teams                            │
│         │                                                           │
│         ▼                                                           │
│  4. Your app receives notification with message data               │
│         │                                                           │
│         ▼                                                           │
│  5. Your app processes message (AI response, logging, etc.)        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Architecture

```
Teams User
    │
    │ Sends chat message
    ▼
┌─────────────────────────┐
│   Microsoft Teams       │
│   (Microsoft 365)       │
└─────────────────────────┘
    │
    │ Change detected
    ▼
┌─────────────────────────┐
│   Microsoft Graph API   │
│   (Change Notifications)│
└─────────────────────────┘
    │
    │ HTTP POST webhook
    ▼
┌─────────────────────────┐
│   Your Application      │
│   /webhooks/teams       │
└─────────────────────────┘
    │
    │ Process & respond
    ▼
┌─────────────────────────┐
│   AI Agent / Response   │
│   (Optional)            │
└─────────────────────────┘
```

### Notification Types

| Resource | Description | Use Case |
|----------|-------------|----------|
| `/chats/{id}/messages` | Messages in a specific chat | Monitor specific conversations |
| `/chats/getAllMessages` | All messages across all chats | Org-wide monitoring (requires admin) |
| `/teams/{id}/channels/{id}/messages` | Channel messages | Monitor team channels |
| `/users/{id}/chats/getAllMessages` | All chats for a user | User-specific monitoring |

---

## Prerequisites

- [ ] **Microsoft 365 tenant** with Teams enabled
- [ ] **Azure AD** access (to register applications)
- [ ] **Admin consent** for certain permissions
- [ ] **HTTPS endpoint** for webhooks (Microsoft requires HTTPS)
- [ ] **Public URL** accessible from the internet

### Licensing Notes

| Feature | License Required |
|---------|------------------|
| Chat message notifications | Microsoft 365 Business Basic or higher |
| `getAllMessages` (tenant-wide) | Additional compliance license may be required |
| Channel message notifications | Standard Teams license |

---

## Step 1: Register Azure AD Application

### 1.1 Create App Registration

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** → **App registrations**
3. Click **+ New registration**
4. Configure:

```
Name: Teams Chat Notifications
Supported account types: Accounts in this organizational directory only
Redirect URI: (leave blank for now)
```

5. Click **Register**
6. Note down:
   - **Application (client) ID**: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
   - **Directory (tenant) ID**: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

### 1.2 Create Client Secret

1. In your app registration, go to **Certificates & secrets**
2. Click **+ New client secret**
3. Add description: `Teams Notifications Secret`
4. Choose expiration (recommended: 24 months)
5. Click **Add**
6. **Copy the secret value immediately** (you won't see it again)

---

## Step 2: Configure API Permissions

### 2.1 Add Required Permissions (Delegated)

We use **Delegated permissions** so the app acts as a specific user, accessing only their chats.

1. In your app registration, go to **API permissions**
2. Click **+ Add a permission**
3. Select **Microsoft Graph**
4. Select **Delegated permissions** (NOT Application permissions)

5. Search for and add these permissions:

| Permission | Description |
|------------|-------------|
| `Chat.Read` | Read user's chat messages |
| `Chat.ReadWrite` | Read and send messages (if you want to reply) |
| `offline_access` | Allows refresh tokens for long-lived access |
| `User.Read` | Read user's basic profile |

6. Click **Add permissions**

> **Note:** Delegated permissions do NOT require admin consent - the user can consent for themselves during the OAuth flow.

### 2.2 Configure Redirect URI

Since we're using delegated permissions, we need an OAuth flow:

1. In your app registration, go to **Authentication**
2. Click **+ Add a platform**
3. Select **Web**
4. Add your redirect URI: `https://your-app.com/auth/callback`
5. Click **Configure**

### Why Delegated (not Application) Permissions?

| Approach | What it accesses | For Teams Chat |
|----------|------------------|----------------|
| **Application permissions** | ALL chats in entire tenant | Overkill, requires admin |
| **Delegated permissions** | Only the authorized user's chats | ✅ Recommended |

With delegated permissions:
- User authorizes once via OAuth
- You store their refresh token
- App monitors only that user's chats
- No admin consent needed

---

## Step 3: Authorize the User (OAuth Flow)

Since we're using delegated permissions, the target user must authorize your app once.

### 3.1 Build the Authorization URL

```python
import urllib.parse

def get_auth_url(client_id: str, redirect_uri: str, tenant_id: str) -> str:
    """Generate the OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "offline_access Chat.Read Chat.ReadWrite User.Read",
        "response_mode": "query",
    }
    base_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    return f"{base_url}?{urllib.parse.urlencode(params)}"
```

### 3.2 Handle the Callback

```python
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import httpx

app = FastAPI()

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str):
    """Exchange authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

        if response.status_code == 200:
            tokens = response.json()
            # Store these securely!
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]

            # Save refresh_token to database for later use
            await save_refresh_token(user_email, refresh_token)

            return {"status": "authorized", "message": "Teams chat access granted"}
        else:
            return {"error": response.text}
```

### 3.3 Refresh the Access Token

Access tokens expire after ~1 hour. Use the refresh token to get new ones:

```python
async def refresh_access_token(refresh_token: str) -> dict:
    """Get a new access token using the refresh token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "offline_access Chat.Read Chat.ReadWrite User.Read",
            },
        )

        if response.status_code == 200:
            tokens = response.json()
            # Update stored refresh token (it may have been rotated)
            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", refresh_token),
            }
        else:
            raise Exception(f"Token refresh failed: {response.text}")
```

---

## Step 4: Set Up Webhook Endpoint

Your application needs an HTTPS endpoint that Microsoft Graph will call when notifications occur.

### 3.1 Webhook Requirements

| Requirement | Details |
|-------------|---------|
| **Protocol** | HTTPS only (HTTP not allowed) |
| **Response** | Must respond with 200 OK within 3 seconds |
| **Validation** | Must echo `validationToken` on subscription creation |
| **Availability** | Must be publicly accessible |

### 3.2 Create Webhook Handler (Python/FastAPI Example)

```python
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import json

app = FastAPI()

class ChangeNotification(BaseModel):
    subscriptionId: str
    changeType: str
    resource: str
    resourceData: Optional[dict] = None
    clientState: Optional[str] = None
    tenantId: str

class NotificationPayload(BaseModel):
    value: List[ChangeNotification]

@app.post("/webhooks/teams")
async def teams_webhook(request: Request):
    """
    Handle Microsoft Graph change notifications for Teams.
    """
    # Check for validation request (subscription creation)
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        # Echo back the validation token as plain text
        return Response(
            content=validation_token,
            media_type="text/plain",
            status_code=200
        )

    # Parse notification payload
    try:
        body = await request.json()
        notifications = body.get("value", [])

        for notification in notifications:
            # Verify client state if you set one
            if notification.get("clientState") != "your-secret-state":
                print("Warning: Client state mismatch")
                continue

            # Process the notification
            await process_teams_notification(notification)

        # Must respond with 202 Accepted quickly
        return Response(status_code=202)

    except Exception as e:
        print(f"Error processing notification: {e}")
        # Still return 202 to acknowledge receipt
        return Response(status_code=202)

async def process_teams_notification(notification: dict):
    """
    Process a single Teams notification.
    """
    change_type = notification.get("changeType")
    resource = notification.get("resource")
    resource_data = notification.get("resourceData", {})

    print(f"Received {change_type} notification for {resource}")

    if change_type == "created":
        # New message created
        message_id = resource_data.get("id")
        chat_id = resource_data.get("chatId")

        # Fetch full message content using Graph API
        # (notification may not include full content)
        await fetch_and_process_message(chat_id, message_id)

async def fetch_and_process_message(chat_id: str, message_id: str):
    """
    Fetch the full message content from Graph API and process it.
    """
    # Implementation depends on your auth setup
    # See Code Examples section for full implementation
    pass
```

### 3.3 Expose Your Endpoint

For development, use a tunneling service:

```bash
# Using ngrok
ngrok http 8000

# You'll get a URL like: https://abc123.ngrok.io
# Your webhook URL: https://abc123.ngrok.io/webhooks/teams
```

For production, deploy to a cloud service with HTTPS (Cloud Run, Azure Functions, etc.)

---

## Step 5: Create Change Notification Subscription

### 5.1 Subscription Basics

A subscription tells Microsoft Graph:
- **What** to watch (resource path)
- **Where** to send notifications (your webhook URL)
- **How long** to watch (max 60 minutes for most resources, can be renewed)

### 5.2 Create Subscription via Graph API (Delegated)

With delegated permissions, you subscribe to the **user's chats** using their access token:

```python
import httpx
from datetime import datetime, timedelta

async def create_chat_subscription(user_access_token: str, webhook_url: str):
    """
    Create a subscription for the user's chat message notifications.
    Uses the user's access token (delegated permissions).
    """
    # Subscription expires in 60 minutes (max for chat messages)
    expiration = datetime.utcnow() + timedelta(minutes=60)

    subscription_data = {
        "changeType": "created",  # Watch for new messages
        "notificationUrl": webhook_url,
        "resource": "/me/chats/getAllMessages",  # User's chats only (delegated)
        "expirationDateTime": expiration.isoformat() + "Z",
        "clientState": "your-secret-state",  # Optional: verify notifications
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers={
                "Authorization": f"Bearer {user_access_token}",  # User's token
                "Content-Type": "application/json",
            },
            json=subscription_data,
        )

        if response.status_code == 201:
            subscription = response.json()
            print(f"Subscription created: {subscription['id']}")
            print(f"Expires: {subscription['expirationDateTime']}")
            return subscription
        else:
            print(f"Error: {response.status_code} - {response.text}")
            raise Exception(f"Failed to create subscription: {response.text}")
```

> **Key difference:** We use `/me/chats/getAllMessages` with the user's token, not `/chats/getAllMessages` with an app token. This automatically scopes to only that user's chats.

### 5.3 Subscription Without Resource Data (Simpler)

If you don't need message content in the notification (you'll fetch it separately):

```python
subscription_data = {
    "changeType": "created",
    "notificationUrl": webhook_url,
    "resource": "/chats/getAllMessages",
    "expirationDateTime": expiration.isoformat() + "Z",
    "clientState": "your-secret-state",
}
```

This is simpler as it doesn't require encryption certificates.

---

## Step 6: Handle Incoming Notifications

### 6.1 Notification Payload Structure

When a chat message is created, you'll receive:

```json
{
    "value": [
        {
            "subscriptionId": "subscription-id",
            "changeType": "created",
            "resource": "chats/chat-id/messages/message-id",
            "resourceData": {
                "@odata.type": "#Microsoft.Graph.chatMessage",
                "@odata.id": "chats/chat-id/messages/message-id",
                "id": "message-id",
                "chatId": "chat-id"
            },
            "clientState": "your-secret-state",
            "tenantId": "tenant-id",
            "subscriptionExpirationDateTime": "2024-01-01T00:00:00Z"
        }
    ]
}
```

### 6.2 Fetch Full Message Content

The notification typically doesn't include full message content. Fetch it:

```python
async def fetch_message(access_token: str, chat_id: str, message_id: str):
    """
    Fetch the full message content from Graph API.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
        )

        if response.status_code == 200:
            message = response.json()
            return {
                "id": message["id"],
                "sender": message["from"]["user"]["displayName"],
                "sender_id": message["from"]["user"]["id"],
                "content": message["body"]["content"],
                "content_type": message["body"]["contentType"],
                "created": message["createdDateTime"],
                "chat_id": chat_id,
            }
        else:
            print(f"Error fetching message: {response.status_code}")
            return None
```

### 6.3 Filter and Process Messages

```python
async def process_teams_notification(notification: dict, access_token: str):
    """
    Process a Teams chat notification.
    """
    change_type = notification.get("changeType")
    resource = notification.get("resource")

    if change_type != "created":
        return  # Only process new messages

    # Extract IDs from resource path
    # Format: "chats/{chat-id}/messages/{message-id}"
    parts = resource.split("/")
    chat_id = parts[1]
    message_id = parts[3]

    # Fetch full message
    message = await fetch_message(access_token, chat_id, message_id)

    if not message:
        return

    # Skip messages from bots or your own app
    if message["sender_id"] == "your-app-user-id":
        return

    print(f"New message from {message['sender']}: {message['content']}")

    # Process the message (e.g., send to AI agent)
    await handle_chat_message(message)
```

---

## Step 7: Renew Subscriptions

Subscriptions expire (max 60 minutes for chat messages). You must renew them before expiration.

### 7.1 Renewal Strategy

```python
import asyncio
from datetime import datetime, timedelta

class SubscriptionManager:
    def __init__(self, access_token_provider):
        self.subscriptions = {}
        self.access_token_provider = access_token_provider

    async def renew_subscription(self, subscription_id: str):
        """
        Renew a subscription before it expires.
        """
        access_token = await self.access_token_provider()
        new_expiration = datetime.utcnow() + timedelta(minutes=60)

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "expirationDateTime": new_expiration.isoformat() + "Z"
                },
            )

            if response.status_code == 200:
                print(f"Subscription {subscription_id} renewed until {new_expiration}")
                return response.json()
            else:
                print(f"Failed to renew subscription: {response.text}")
                raise Exception(f"Renewal failed: {response.text}")

    async def start_renewal_loop(self, subscription_id: str):
        """
        Background task to renew subscription every 50 minutes.
        """
        while True:
            await asyncio.sleep(50 * 60)  # 50 minutes
            try:
                await self.renew_subscription(subscription_id)
            except Exception as e:
                print(f"Renewal error: {e}")
                # Re-create subscription if renewal fails
                await self.recreate_subscription()
```

### 7.2 Handle Lifecycle Notifications

Microsoft sends lifecycle notifications when subscriptions are about to expire:

```python
@app.post("/webhooks/teams")
async def teams_webhook(request: Request):
    body = await request.json()

    # Check for lifecycle notifications
    lifecycle_notifications = body.get("lifecycleNotifications", [])
    for lifecycle in lifecycle_notifications:
        if lifecycle.get("lifecycleEvent") == "reauthorizationRequired":
            # Token expired, need to reauthorize
            await handle_reauthorization(lifecycle["subscriptionId"])
        elif lifecycle.get("lifecycleEvent") == "subscriptionRemoved":
            # Subscription was removed, recreate it
            await recreate_subscription()

    # Handle regular notifications
    notifications = body.get("value", [])
    # ... process notifications
```

---

## Step 8: Sending Replies (Optional)

If you want to reply to chat messages:

```python
async def send_chat_reply(access_token: str, chat_id: str, message: str):
    """
    Send a reply to a Teams chat.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "body": {
                    "content": message,
                    "contentType": "text",  # or "html"
                }
            },
        )

        if response.status_code == 201:
            print("Message sent successfully")
            return response.json()
        else:
            print(f"Error sending message: {response.status_code} - {response.text}")
            return None
```

---

## Troubleshooting

### Subscription Creation Fails with 400 Bad Request

**Symptoms:** `"The value of the resource property is invalid"`

**Solutions:**
1. Verify the resource path is correct (e.g., `/chats/getAllMessages`)
2. Check you have the required permissions
3. Ensure admin consent has been granted

### Webhook Not Receiving Notifications

**Symptoms:** Subscription created successfully but no notifications arrive

**Solutions:**
1. Verify your webhook URL is publicly accessible
2. Check your endpoint returns 200/202 quickly (< 3 seconds)
3. Test with a simple message in Teams
4. Check your subscription hasn't expired

### Validation Token Not Working

**Symptoms:** `"Subscription validation request failed"`

**Solutions:**
1. Return the `validationToken` as **plain text**, not JSON
2. Return exactly the token value, no extra characters
3. Respond with 200 OK, not 202 Accepted
4. Example:
   ```python
   return Response(content=validation_token, media_type="text/plain")
   ```

### Permission Denied Errors

**Symptoms:** 403 Forbidden when creating subscription or fetching messages

**Solutions:**
1. Verify API permissions are correctly configured
2. Ensure admin consent is granted
3. Check the access token has the required scopes
4. For application permissions, verify the app is authorized

### Subscription Expires Quickly

**Symptoms:** Subscription stops working after ~1 hour

**Solutions:**
1. Implement subscription renewal (see Step 6)
2. Set up a background task to renew every 50 minutes
3. Handle lifecycle notifications for reauthorization

---

## Code Examples

### Complete Webhook Handler

```python
# teams_webhook.py

from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager
import httpx
import asyncio
import os
from datetime import datetime, timedelta

app = FastAPI()

# Configuration
TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://your-app.com/webhooks/teams
CLIENT_STATE = os.getenv("CLIENT_STATE", "teams-notifications-secret")

# Global state
subscription_id = None
access_token = None
token_expires_at = None


async def get_access_token():
    """Get or refresh Microsoft Graph access token."""
    global access_token, token_expires_at

    if access_token and token_expires_at and datetime.utcnow() < token_expires_at:
        return access_token

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )

        if response.status_code == 200:
            data = response.json()
            access_token = data["access_token"]
            token_expires_at = datetime.utcnow() + timedelta(seconds=data["expires_in"] - 300)
            return access_token
        else:
            raise Exception(f"Failed to get access token: {response.text}")


async def create_subscription():
    """Create a new subscription for chat messages."""
    global subscription_id

    token = await get_access_token()
    expiration = datetime.utcnow() + timedelta(minutes=60)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "changeType": "created",
                "notificationUrl": WEBHOOK_URL,
                "resource": "/chats/getAllMessages",
                "expirationDateTime": expiration.isoformat() + "Z",
                "clientState": CLIENT_STATE,
            },
        )

        if response.status_code == 201:
            data = response.json()
            subscription_id = data["id"]
            print(f"Subscription created: {subscription_id}")
            return data
        else:
            print(f"Failed to create subscription: {response.text}")
            raise Exception(response.text)


async def renew_subscription():
    """Renew the current subscription."""
    global subscription_id

    if not subscription_id:
        return await create_subscription()

    token = await get_access_token()
    expiration = datetime.utcnow() + timedelta(minutes=60)

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "expirationDateTime": expiration.isoformat() + "Z"
            },
        )

        if response.status_code == 200:
            print(f"Subscription renewed: {subscription_id}")
            return response.json()
        else:
            # Subscription might have expired, create new one
            print(f"Renewal failed, creating new subscription")
            return await create_subscription()


async def renewal_loop():
    """Background task to renew subscription every 50 minutes."""
    while True:
        await asyncio.sleep(50 * 60)
        try:
            await renew_subscription()
        except Exception as e:
            print(f"Renewal error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup: create subscription and start renewal loop
    await create_subscription()
    asyncio.create_task(renewal_loop())
    yield
    # Shutdown: could delete subscription here if desired


app = FastAPI(lifespan=lifespan)


@app.post("/webhooks/teams")
async def teams_webhook(request: Request):
    """Handle Teams webhook notifications."""

    # Validation request
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain")

    # Process notifications
    try:
        body = await request.json()
        notifications = body.get("value", [])

        for notification in notifications:
            # Verify client state
            if notification.get("clientState") != CLIENT_STATE:
                continue

            # Process in background to respond quickly
            asyncio.create_task(process_notification(notification))

        return Response(status_code=202)

    except Exception as e:
        print(f"Error: {e}")
        return Response(status_code=202)


async def process_notification(notification: dict):
    """Process a single notification."""
    try:
        change_type = notification.get("changeType")
        resource = notification.get("resource")

        if change_type != "created":
            return

        # Parse resource path: chats/{chatId}/messages/{messageId}
        parts = resource.split("/")
        if len(parts) >= 4:
            chat_id = parts[1]
            message_id = parts[3]

            # Fetch full message
            token = await get_access_token()
            message = await fetch_message(token, chat_id, message_id)

            if message:
                print(f"New message from {message['sender']}: {message['content'][:100]}")
                # TODO: Process message (send to AI, log, etc.)

    except Exception as e:
        print(f"Error processing notification: {e}")


async def fetch_message(token: str, chat_id: str, message_id: str) -> dict:
    """Fetch message content from Graph API."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "id": data["id"],
                "sender": data.get("from", {}).get("user", {}).get("displayName", "Unknown"),
                "sender_id": data.get("from", {}).get("user", {}).get("id"),
                "content": data.get("body", {}).get("content", ""),
                "content_type": data.get("body", {}).get("contentType", "text"),
                "created": data.get("createdDateTime"),
                "chat_id": chat_id,
            }
        return None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Environment Variables

```bash
# .env file

# Azure AD Application
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret

# Webhook Configuration
WEBHOOK_URL=https://your-app.com/webhooks/teams
CLIENT_STATE=your-secret-state-for-validation
```

---

## Useful Links

### Microsoft Documentation
- [Microsoft Graph Change Notifications Overview](https://learn.microsoft.com/en-us/graph/api/resources/webhooks)
- [Chat Message Subscriptions](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage)
- [Graph API Permissions Reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
- [Subscription Resource Type](https://learn.microsoft.com/en-us/graph/api/resources/subscription)

### Tools
- [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) - Test API calls
- [ngrok](https://ngrok.com/) - Tunnel for local development

---

## Next Steps

1. **Register your Azure AD application** with required permissions
2. **Set up your webhook endpoint** with HTTPS
3. **Create a subscription** for chat messages
4. **Handle notifications** and process messages
5. **Integrate with your AI agent** for automated responses

For the Teams meeting integration, see [TEAMS_MEETING_GUIDE.md](./TEAMS_MEETING_GUIDE.md).
