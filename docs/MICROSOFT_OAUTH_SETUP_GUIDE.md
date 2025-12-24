# Microsoft OAuth Setup Guide

This guide covers the common OAuth setup for all Microsoft 365 integrations (Outlook, Teams Chat, etc.).

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Customer Setup: Azure AD App](#customer-setup-azure-ad-app)
3. [OAuth Authorization Flow](#oauth-authorization-flow)
4. [Token Storage](#token-storage)
5. [API Reference](#api-reference)

---

## Architecture Overview

Each customer creates their own Azure AD app in their Microsoft 365 tenant:

```
┌─────────────────────────────────────────────────────────────────────┐
│ Customer's Microsoft 365 Tenant                                     │
│                                                                     │
│   Azure AD App (created by customer)                                │
│   ├── tenant_id: "abc123-..."                                       │
│   ├── client_id: "def456-..."                                       │
│   ├── client_secret: "xyz789..."                                    │
│   └── Permissions: Mail.Send, Chat.ReadWrite, etc.                  │
│                                                                     │
│   Service Account (user who authorizes)                             │
│   └── unify-bot@company.com                                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ OAuth (delegated permissions)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Unify Platform                                                      │
│                                                                     │
│   Stores: { user_email, tenant_id, client_id, client_secret,        │
│             access_token, refresh_token }                           │
│                                                                     │
│   Can now act as unify-bot@company.com:                             │
│   ├── Send emails                                                   │
│   ├── Read/send Teams messages                                      │
│   └── etc.                                                          │
└─────────────────────────────────────────────────────────────────────┘
```

**Why per-customer apps?**
- Customer maintains full control
- Customer can revoke access anytime from their Azure portal
- No shared credentials between customers
- Customer chooses which permissions to grant

---

## Customer Setup: Azure AD App

### Step 1: Access Azure Portal

1. Go to [https://portal.azure.com](https://portal.azure.com)
2. Sign in with Microsoft 365 admin account
3. Search for **"Azure Active Directory"** (or "Microsoft Entra ID")

### Step 2: Register Application

1. Click **"App registrations"** → **"+ New registration"**

2. Configure:
   - **Name:** `Unify Integration`
   - **Supported account types:** "Accounts in this organizational directory only"
   - **Redirect URI:** 
     - Platform: `Web`
     - URL: `https://adapters.unify.ai/microsoft/auth/callback` (or your callback URL)

3. Click **"Register"**

4. Note down:
   - **Application (client) ID** → `client_id`
   - **Directory (tenant) ID** → `tenant_id`

### Step 3: Create Client Secret

1. Go to **"Certificates & secrets"** → **"+ New client secret"**
2. Add description: `Unify Integration`
3. Choose expiration (recommended: 24 months)
4. Click **"Add"**
5. **Copy the secret value immediately** → `client_secret`

> ⚠️ The secret is only shown once. Copy it now!

### Step 4: Configure API Permissions

1. Go to **"API permissions"** → **"+ Add a permission"** → **"Microsoft Graph"**
2. Select **"Delegated permissions"**
3. Add permissions based on integration:

| Integration | Required Permissions |
|-------------|---------------------|
| Outlook Email | `Mail.Read`, `Mail.Send`, `Mail.ReadWrite`, `User.Read`, `offline_access` |
| Teams Chat | `Chat.Read`, `Chat.ReadWrite`, `User.Read`, `offline_access` |
| Both | All of the above |

4. Click **"Grant admin consent for [Organization]"** (requires admin)

### Step 5: Provide Credentials to Unify

Customer provides to Unify:
```json
{
  "tenant_id": "abc123-...",
  "client_id": "def456-...",
  "client_secret": "xyz789..."
}
```

These are stored securely in Unify's database.

---

## OAuth Authorization Flow

After the customer registers their app and provides credentials, a user from their organization authorizes the integration.

### Flow Diagram

```
┌──────────────────┐
│ 1. Generate URL  │  Construct OAuth URL with tenant_id, client_id
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│ 2. User visits URL                                               │
│                                                                  │
│    https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/    │
│    authorize?client_id={client_id}&response_type=code&...        │
│                                                                  │
│    - User logs in (e.g., unify-bot@company.com)                  │
│    - User sees consent screen                                    │
│    - User clicks "Accept"                                        │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│ 3. Microsoft redirects to callback                               │
│                                                                  │
│    GET /microsoft/auth/callback?code=ABC123&state=...            │
│                                                                  │
│    Callback endpoint:                                            │
│    - Looks up client_secret from database                        │
│    - Exchanges code for tokens                                   │
│    - Gets user email from token                                  │
│    - Stores tokens in database                                   │
│    - Redirects to success page                                   │
└──────────────────────────────────────────────────────────────────┘
```

### Constructing the OAuth URL

```
https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?
  client_id={client_id}
  &response_type=code
  &redirect_uri=https://adapters.unify.ai/microsoft/auth/callback
  &scope={client_id}/.default
  &response_mode=query
  &state={base64_encoded_state}
```

**Important:** The `state` parameter is REQUIRED. It contains the tenant_id and client_id that the callback needs to look up credentials.

**Generating the state parameter (Python):**
```python
import base64
import json

state_data = {
    "tenant_id": "your-tenant-id",
    "client_id": "your-client-id",
    "redirect_after": "https://your-app.com/success"  # optional
}
state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
```

| Parameter | Description |
|-----------|-------------|
| `tenant_id` | Customer's Azure AD tenant ID |
| `client_id` | Customer's app client ID |
| `redirect_uri` | Must match URI configured in app registration (adapters URL) |
| `scope` | `{client_id}/.default` requests all configured permissions |
| `state` | **Required.** Base64-encoded JSON with tenant_id, client_id, redirect_after |

**Example:**
```
https://login.microsoftonline.com/abc123-tenant-id/oauth2/v2.0/authorize?
  client_id=def456-client-id
  &response_type=code
  &redirect_uri=https://adapters.unify.ai/microsoft/auth/callback
  &scope=def456-client-id/.default
  &response_mode=query
```

You can send this URL to the customer via email, embed it in a dashboard, etc.

---

## Token Storage

After successful authorization, Unify stores:

```json
{
  "user_email": "unify-bot@company.com",
  "tenant_id": "abc123-...",
  "client_id": "def456-...",
  "client_secret": "xyz789...",
  "access_token": "eyJ...",
  "refresh_token": "0.AR...",
  "expires_at": "2024-01-15T12:00:00Z"
}
```

### Token Lifecycle

- **Access tokens** expire in ~1 hour
- **Refresh tokens** expire in 90 days if unused
- A daily background job "touches" all tokens to keep them alive indefinitely
- When making API calls, tokens are refreshed automatically if expired

### Revoking Access

Customer can revoke access anytime:
1. Azure Portal → Enterprise Applications → Find app → Users → Revoke
2. Or delete the app registration entirely

---

## API Reference

### Callback Endpoint

```
GET /microsoft/auth/callback
```

Microsoft redirects here after user authorizes. This endpoint:
1. Extracts `code` from query params
2. Looks up `client_secret` from database (using tenant_id/client_id)
3. Exchanges code for tokens
4. Stores tokens
5. Redirects to success page

**Query Parameters (from Microsoft):**
| Parameter | Description |
|-----------|-------------|
| `code` | Authorization code to exchange for tokens |
| `state` | Encoded state (contains tenant_id, client_id, redirect_after) |
| `error` | Error code if authorization failed |
| `error_description` | Error details |

**Success Response:**
Redirects to `redirect_after?success=true&user_email=...`

Or if no redirect_after:
```json
{
  "success": true,
  "user_email": "unify-bot@company.com",
  "stored": true
}
```

---

## Environment Variables

```bash
# Adapters URL (callback URL must match Azure AD app registration)
UNITY_ADAPTERS_URL=https://adapters.unify.ai

# External storage API
UNIFY_BASE_URL=https://api.unify.ai/v0
```

---

## External Storage API

Unify's backend needs these endpoints:

### Store Credentials (during onboarding)
```
POST /microsoft/credentials
{
  "tenant_id": "...",
  "client_id": "...",
  "client_secret": "..."
}
```

### Get Credentials (for OAuth callback)
```
GET /microsoft/credentials?tenant_id=...&client_id=...
→ { "client_secret": "..." }
```

### Store Token (after OAuth)
```
POST /microsoft/token
{
  "user_email": "...",
  "tenant_id": "...",
  "client_id": "...",
  "client_secret": "...",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": "..."
}
```

### Get Token (for API calls)
```
GET /microsoft/token?user_email=...
→ { access_token, refresh_token, tenant_id, client_id, client_secret, expires_at }
```

---

## Next Steps

- [Outlook Email Integration](./OUTLOOK_SETUP_GUIDE.md) - Email sending/receiving
- [Teams Chat Integration](./TEAMS_CHAT_NOTIFICATIONS_GUIDE.md) - Chat messaging

---

## Troubleshooting

### "AADSTS50011: Reply URL does not match"
The `redirect_uri` in your OAuth URL doesn't match what's configured in the Azure AD app.

**Fix:** Go to Azure Portal → App Registration → Authentication → Add the exact redirect URI.

### "AADSTS65001: User or admin has not consented"
The required permissions haven't been granted.

**Fix:** Azure Portal → App Registration → API Permissions → "Grant admin consent"

### "AADSTS7000218: Invalid client secret"
The client_secret is wrong or expired.

**Fix:** Create a new client secret in Azure Portal and update your stored credentials.

### Tokens expiring / refresh failing
Refresh tokens expire after 90 days of inactivity.

**Fix:** Ensure the daily token refresh job is running to keep tokens alive.

