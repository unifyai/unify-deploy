# Microsoft Outlook Email Integration Setup Guide

This guide walks you through setting up Microsoft Graph API for Outlook email integration with Unify.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Step 1: Access Azure Portal](#step-1-access-azure-portal)
3. [Step 2: Register an Application](#step-2-register-an-application)
4. [Step 3: Configure API Permissions (Delegated)](#step-3-configure-api-permissions)
5. [Step 4: Create Client Secret](#step-4-create-client-secret)
6. [Step 5: Configure Environment Variables](#step-5-configure-environment-variables)
7. [Step 5.5: Configure Mail Routing for Verified Domains](#step-55-configure-mail-routing-for-verified-domains-important)
8. [Step 6: Authorize the User (OAuth Flow)](#step-6-authorize-the-user-oauth-flow)
9. [Step 7: Set Up Webhook Endpoint](#step-7-set-up-webhook-endpoint)
10. [Step 8: Test the Integration](#step-8-test-the-integration)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Microsoft 365 account** with admin access (Business Basic or higher)
- **A publicly accessible HTTPS endpoint** for webhooks (required for receiving email notifications)
- Access to your deployment environment to set environment variables

> **Note:** You do NOT need a paid Azure subscription. Azure AD (Microsoft Entra ID) is included with your Microsoft 365 subscription.

---

## Step 1: Access Azure Portal

1. Go to [https://portal.azure.com](https://portal.azure.com)
2. Sign in with your **Microsoft 365 admin account**
3. In the search bar, type **"Azure Active Directory"** (or "Microsoft Entra ID")
4. Click on it to open the directory management

![Azure Portal Search](https://learn.microsoft.com/en-us/azure/active-directory/develop/media/quickstart-register-app/portal-aad.png)

---

## Step 2: Register an Application

1. In Azure Active Directory, click **"App registrations"** in the left sidebar
2. Click **"+ New registration"** at the top

3. Fill in the registration form:
   - **Name:** `Unify Email Integration` (or any descriptive name)
   - **Supported account types:** Select based on your needs:
     - *"Accounts in this organizational directory only"* - For single tenant (recommended)
     - *"Accounts in any organizational directory"* - For multi-tenant
   - **Redirect URI:** Leave blank for now (not needed for this integration)

4. Click **"Register"**

5. **Copy these values** (you'll need them later):
   - **Application (client) ID** → This is your `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → This is your `AZURE_TENANT_ID`

![App Registration Overview](https://learn.microsoft.com/en-us/azure/active-directory/develop/media/quickstart-register-app/portal-app-registration.png)

> ### 📧 One App, Multiple Users
>
> A single app registration can be used by **multiple users** - each user authorizes the app separately.
>
> | Approach | How It Works |
> |----------|--------------|
> | **Delegated (recommended)** | Each user authorizes → app accesses only their mailbox |
> | **Application** | Admin consents → app can access all mailboxes (use access policies to restrict) |
>
> With delegated permissions:
> - User A authorizes → app monitors User A's inbox
> - User B authorizes → app monitors User B's inbox
> - Each user's refresh token stored separately
>
> With application permissions (requires admin):
> - App can access any mailbox by specifying email address
> - Use [Application Access Policies](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access) to restrict

---

## Step 3: Configure API Permissions

We recommend **Delegated permissions** for a consistent pattern with Teams Chat integration.

1. In your app registration, click **"API permissions"** in the left sidebar
2. Click **"+ Add a permission"**
3. Select **"Microsoft Graph"**
4. Select **"Delegated permissions"**

5. Search for and add these permissions:

   | Permission | Description |
   |------------|-------------|
   | `Mail.Read` | Read user's mail |
   | `Mail.Send` | Send mail as the user |
   | `Mail.ReadWrite` | Read and write user's mail |
   | `offline_access` | Maintain access (refresh tokens) |
   | `User.Read` | Read user's basic profile |

6. Click **"Add permissions"**

> **Note:** Delegated permissions do NOT require admin consent - the user can consent for themselves during the OAuth flow.

### Alternative: Application Permissions (Tenant-Wide Access)

If you need to access multiple mailboxes without individual user consent, use Application permissions instead:

<details>
<summary>Click to expand Application permissions setup</summary>

1. Select **"Application permissions"** (instead of Delegated)
2. Add these permissions:
   - `Mail.Read` - Read mail in all mailboxes
   - `Mail.Send` - Send mail as any user
   - `Mail.ReadWrite` - Read and write mail in all mailboxes

3. Click **"Grant admin consent for [Your Organization]"**
   - Requires admin privileges
   - All permissions should show a green checkmark ✅

4. To restrict to specific mailboxes, configure [Application Access Policies](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access)

</details>

![API Permissions](https://learn.microsoft.com/en-us/azure/active-directory/develop/media/quickstart-configure-app-access-web-apis/portal-permissions.png)

---

## Step 3.5: Configure Redirect URI

Since we're using delegated permissions, we need an OAuth flow:

1. In your app registration, go to **Authentication**
2. Click **+ Add a platform**
3. Select **Web**
4. Add your redirect URI: `https://your-domain.com/outlook/auth/callback`
5. Click **Configure**

---

## Step 4: Create Client Secret

1. In your app registration, click **"Certificates & secrets"** in the left sidebar
2. Under "Client secrets", click **"+ New client secret"**

3. Fill in the form:
   - **Description:** `Unify Production` (or any descriptive name)
   - **Expires:** Choose an expiration period
     - Recommended: 24 months for production
     - Set a calendar reminder to rotate before expiry!

4. Click **"Add"**

5. **IMPORTANT: Copy the secret value NOW**
   - The value is only shown once!
   - This is your `AZURE_CLIENT_SECRET`
   - If you lose it, you'll need to create a new secret

![Client Secret](https://learn.microsoft.com/en-us/azure/active-directory/develop/media/quickstart-register-app/portal-client-secret.png)

---

## Step 5: Configure Environment Variables

Add these environment variables to your `.env` file or deployment configuration:

```bash
# Microsoft Graph API Configuration
AZURE_TENANT_ID=your-tenant-id-here
AZURE_CLIENT_ID=your-client-id-here
AZURE_CLIENT_SECRET=your-client-secret-here

# Webhook Configuration (optional, for security)
OUTLOOK_WEBHOOK_SECRET=your-random-secret-string

# Your application's public URL (for webhook callbacks)
DROID_COMMS_URL=https://your-domain.com
```

### Where to find each value:

| Variable | Where to Find |
|----------|---------------|
| `AZURE_TENANT_ID` | App registration → Overview → "Directory (tenant) ID" |
| `AZURE_CLIENT_ID` | App registration → Overview → "Application (client) ID" |
| `AZURE_CLIENT_SECRET` | The value you copied in Step 4 |
| `OUTLOOK_WEBHOOK_SECRET` | Generate a random string (e.g., `openssl rand -hex 32`) |

---

## Step 5.5: Configure Mail Routing for Verified Domains (Important)

If you have verified a domain (e.g., `unify.ai`) in your Microsoft 365 tenant for any reason (SSO, federation, ownership claim, etc.) but the actual mailboxes for that domain are hosted elsewhere (another Microsoft 365 tenant, Google Workspace, etc.), you need to configure mail routing to prevent delivery failures.

### The Problem

When a domain is verified in Microsoft 365, Exchange Online treats it as an **authoritative domain** by default. This means:
- Emails sent to `user@verified-domain.com` are routed internally
- If no mailbox exists in this tenant → `RecipientNotFound` error (550 5.1.10)

### The Solution: Internal Relay Domain

Change the domain type to **Internal Relay**, which tells Exchange:
- "Try internal delivery first, but if no mailbox is found, route externally via MX lookup"

### Configuration Steps

1. Go to **Microsoft 365 Admin Center** → **Admin centers** → **Exchange**
2. Navigate to **Mail flow** → **Accepted domains**
3. Click on your verified domain (e.g., `unify.ai`)
4. Change **Domain type** from **Authoritative** to **Internal Relay**
5. Click **Save**

### Domain Type Comparison

| Domain Type | Behavior |
|-------------|----------|
| **Authoritative** | All mailboxes must exist in this tenant. Reject if not found. |
| **Internal Relay** | Try internal first, forward externally if no mailbox found. |

### Prerequisites

- Ensure MX records for the domain point to the actual mail server (not this tenant)
- The domain remains verified for whatever else relies on it

> **When is this needed?**
> - You have a domain verified in this tenant
> - Email for that domain is hosted elsewhere
> - You want to send emails TO addresses on that domain from this tenant

---

## Step 6: Authorize the User (OAuth Flow)

For delegated permissions, the target user must authorize your app once.

### 6.1 Build the Authorization URL

```python
import urllib.parse

def get_outlook_auth_url(client_id: str, redirect_uri: str, tenant_id: str) -> str:
    """Generate the OAuth authorization URL for Outlook."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "offline_access Mail.Read Mail.Send Mail.ReadWrite User.Read",
        "response_mode": "query",
    }
    base_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    return f"{base_url}?{urllib.parse.urlencode(params)}"

# Example usage:
# auth_url = get_outlook_auth_url(CLIENT_ID, "https://your-app.com/outlook/auth/callback", TENANT_ID)
# Redirect user to auth_url
```

### 6.2 Handle the Callback

```python
from fastapi import FastAPI, Request
import httpx

app = FastAPI()

@app.get("/outlook/auth/callback")
async def outlook_auth_callback(request: Request, code: str):
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
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]

            # Store refresh_token securely for later use
            await save_refresh_token(user_email, refresh_token)

            return {"status": "authorized", "message": "Outlook access granted"}
        else:
            return {"error": response.text}
```

### 6.3 Refresh the Access Token

```python
async def refresh_outlook_token(refresh_token: str) -> dict:
    """Get a new access token using the refresh token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "offline_access Mail.Read Mail.Send Mail.ReadWrite User.Read",
            },
        )

        if response.status_code == 200:
            tokens = response.json()
            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", refresh_token),
            }
        else:
            raise Exception(f"Token refresh failed: {response.text}")
```

---

## Step 7: Set Up Webhook Endpoint

Microsoft Graph uses webhooks to notify your application about new emails. Unlike Gmail's Pub/Sub, you need a publicly accessible HTTPS endpoint.

### 6.1 Ensure Your Endpoint is Accessible

Your `/outlook/webhook` endpoint must be:
- Publicly accessible via HTTPS
- Able to respond within 3 seconds
- Return a 200-202 status code

### 7.2 Register a Subscription

With delegated permissions, you create a subscription using the user's access token. The resource path uses `/me` which automatically scopes to that user:

```python
async def create_mail_subscription(user_access_token: str, webhook_url: str):
    """Create a subscription for the user's inbox notifications."""
    from datetime import datetime, timedelta

    expiration = datetime.utcnow() + timedelta(days=3)  # Max 3 days for mail

    subscription_data = {
        "changeType": "created",
        "notificationUrl": webhook_url,
        "resource": "/me/mailFolders/inbox/messages",  # User's inbox only
        "expirationDateTime": expiration.isoformat() + "Z",
        "clientState": "your-secret-state",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            json=subscription_data,
        )

        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(f"Subscription failed: {response.text}")
```

> **Note:** With delegated permissions and `/me`, the subscription automatically scopes to the authorized user's mailbox only.

### 7.3 Subscription Renewal

**Important:** Microsoft Graph subscriptions expire after **3 days** for mail resources.

Set up a background task to renew subscriptions every 2 days:

```python
async def renew_mail_subscription(user_access_token: str, subscription_id: str):
    """Renew a mail subscription before it expires."""
    from datetime import datetime, timedelta

    new_expiration = datetime.utcnow() + timedelta(days=3)

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "expirationDateTime": new_expiration.isoformat() + "Z"
            },
        )

        if response.status_code == 200:
            print(f"Subscription renewed until {new_expiration}")
            return response.json()
        else:
            raise Exception(f"Renewal failed: {response.text}")
```

---

## Step 8: Test the Integration

### 8.1 Authorize a Test User

1. Generate the authorization URL using your app's credentials
2. Open the URL in a browser and sign in as the test user
3. Grant consent to the requested permissions
4. Verify you receive the tokens in your callback handler

### 8.2 Test Sending an Email

Once authorized, test sending an email as the user:

```python
async def test_send_email(user_access_token: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "subject": "Test from Integration",
                    "body": {"contentType": "Text", "content": "Hello!"},
                    "toRecipients": [{"emailAddress": {"address": "recipient@example.com"}}]
                }
            },
        )
        print(f"Status: {response.status_code}")
```

### 8.3 Test Webhook

1. Create a subscription for the authorized user
2. Send a test email TO the monitored mailbox
3. Check your application logs for the webhook notification

---

## Troubleshooting

### "Insufficient privileges" Error

**Cause:** Admin consent not granted for API permissions.

**Fix:**
1. Go to Azure Portal → App registrations → Your app → API permissions
2. Click "Grant admin consent for [Organization]"
3. Ensure all permissions show green checkmarks

### "Invalid client secret" Error

**Cause:** Client secret is incorrect or expired.

**Fix:**
1. Check that `AZURE_CLIENT_SECRET` is copied correctly (no extra spaces)
2. If expired, create a new secret in Azure Portal
3. Secrets are only shown once - if lost, create a new one

### "Tenant not found" Error

**Cause:** Incorrect tenant ID or wrong account type.

**Fix:**
1. Verify `AZURE_TENANT_ID` matches the "Directory (tenant) ID" in Azure Portal
2. Ensure you registered the app in the correct tenant

### "RecipientNotFound" Error (550 5.1.10)

**Cause:** You're sending to an email address on a domain that's verified in your tenant, but the mailbox doesn't exist in this tenant.

**Symptoms:**
- `send_email` returns success
- Email shows as sent in Outlook
- Delivery failure notification: `550 5.1.10 RESOLVER.ADR.RecipientNotFound`

**Fix:**
1. This commonly happens when a domain is verified in this tenant but mail is hosted elsewhere
2. Go to **Exchange Admin Center** → **Mail flow** → **Accepted domains**
3. Change the domain type from **Authoritative** to **Internal Relay**
4. See [Step 5.5](#step-55-configure-mail-routing-for-verified-domains-important) for detailed instructions

### Webhook Not Receiving Notifications

**Causes & Fixes:**

1. **Endpoint not accessible:**
   - Test your endpoint is reachable: `curl https://your-domain.com/outlook/webhook`
   - Ensure HTTPS with valid certificate

2. **Validation failing:**
   - Check logs for Microsoft's validation request
   - Ensure endpoint returns `validationToken` as plain text

3. **Subscription expired:**
   - Subscriptions expire after 3 days
   - Set up automatic renewal

4. **Client state mismatch:**
   - Ensure `OUTLOOK_WEBHOOK_SECRET` matches what was used when creating subscription

### "Request timeout" Errors

**Cause:** Microsoft Graph API is slow or rate limited.

**Fix:**
1. Implement retry logic with exponential backoff
2. Check Microsoft 365 Service Health for outages
3. Consider caching tokens (the SDK handles this automatically)

---

## API Endpoints Reference

Once configured, these endpoints are available:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/outlook/send` | POST | Send an email |
| `/outlook/watch` | POST | Create inbox subscription |
| `/outlook/watch/renew` | POST | Renew subscription |
| `/outlook/watch` | DELETE | Delete subscription |
| `/outlook/webhook` | POST | Receive notifications |
| `/outlook/message` | GET | Get specific message |
| `/outlook/attachment` | GET | Download attachment |
| `/outlook/thread` | GET | Get conversation thread |

---

## Security Best Practices

1. **Rotate client secrets** before expiry (set calendar reminders)
2. **Use least-privilege permissions** - only request what you need
3. **Validate webhook client state** to prevent spoofing
4. **Store secrets securely** - use secret managers, not plain text files
5. **Monitor API usage** in Azure Portal for anomalies
6. **Enable audit logging** in Microsoft 365 compliance center

---

## Useful Links

- [Microsoft Graph API Documentation](https://learn.microsoft.com/en-us/graph/overview)
- [Mail API Reference](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview)
- [Webhooks/Subscriptions Guide](https://learn.microsoft.com/en-us/graph/webhooks)
- [Azure AD App Registration Guide](https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app)
- [Microsoft Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) - Test API calls interactively

---

## Support

If you encounter issues not covered in this guide:

1. Check Microsoft Graph API status: [Microsoft 365 Service Health](https://status.office365.com/)
2. Review Azure AD sign-in logs: Azure Portal → Azure AD → Sign-in logs
3. Test API calls directly: [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer)

---
