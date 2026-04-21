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

> ⚠️ **Important:** If you add new permissions later (e.g., adding Teams Chat to an existing Outlook integration), you must:
> 1. Add the new permissions in Azure Portal
> 2. Grant admin consent for the new permissions
> 3. **Have the user re-authorize** by visiting the OAuth URL again
>
> Existing tokens only contain the permissions that were consented at the time of authorization. New permissions require a fresh OAuth flow to be included in the token.

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
  &scope=https://graph.microsoft.com/.default offline_access
  &response_mode=query
  &state={base64_encoded_state}
```

**Important:**
- The `scope` must be `https://graph.microsoft.com/.default` to get a token that works with Microsoft Graph API
- `offline_access` is needed to get a refresh token
- The `state` parameter is REQUIRED - it contains the assistant_email to look up credentials

**Generating the state parameter (Python):**
```python
import base64
import json

state_data = {
    "assistant_email": "assistant@company.com",  # Required - to look up AZURE_CLIENT_SECRET
    "tenant_id": "your-tenant-id",
    "client_id": "your-client-id",
    "redirect_after": "https://your-app.com/success"  # optional
}
state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
```

> **Note:** The `assistant_email` must be registered in Orchestra with `AZURE_CLIENT_SECRET` in its secrets.

| Parameter | Description |
|-----------|-------------|
| `tenant_id` | Customer's Azure AD tenant ID |
| `client_id` | Customer's app client ID |
| `redirect_uri` | Must match URI configured in app registration (adapters URL) |
| `scope` | `https://graph.microsoft.com/.default offline_access` for Graph API access + refresh tokens |
| `state` | **Required.** Base64-encoded JSON with assistant_email, tenant_id, client_id, redirect_after |

**Example:**
```
https://login.microsoftonline.com/abc123-tenant-id/oauth2/v2.0/authorize?
  client_id=def456-client-id
  &response_type=code
  &redirect_uri=https://adapters.unify.ai/microsoft/auth/callback
  &scope=https://graph.microsoft.com/.default offline_access
  &response_mode=query
  &state={base64_encoded_state}
```

You can send this URL to the customer via email, embed it in a dashboard, etc.

---

## Token Storage

### Pre-configured during onboarding (before OAuth)

These secrets must be added to the assistant **before** the OAuth flow:

| Secret Name | Description |
|-------------|-------------|
| `AZURE_TENANT_ID` | Azure AD tenant ID (customer provides) |
| `AZURE_CLIENT_ID` | Azure AD app client ID (customer provides) |
| `AZURE_CLIENT_SECRET` | Azure AD app client secret (customer provides) |

> ⚠️ The OAuth callback uses these to exchange the authorization code for tokens.
> The scheduled token refresh job also uses these to refresh tokens every 30 minutes.

### Stored during OAuth callback

These secrets are stored automatically after the user completes authorization:

| Secret Name | Description |
|-------------|-------------|
| `MICROSOFT_ACCESS_TOKEN` | Access token for Graph API calls (~1 hour lifetime) |
| `MICROSOFT_REFRESH_TOKEN` | Refresh token for getting new access tokens |
| `MICROSOFT_TOKEN_EXPIRES_AT` | ISO timestamp when access token expires |

### Token Lifecycle

- **Access tokens** expire in 60-90 minutes (Microsoft adds jitter to prevent thundering herd)
- **Refresh tokens** expire in 90 days if unused
- A **scheduled job runs every 30 minutes** to refresh all Microsoft tokens
- This keeps both access tokens and refresh tokens alive indefinitely
- Endpoints (Outlook, Teams) simply use the stored access token - no refresh logic needed

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

# Orchestra API URL
ORCHESTRA_URL=https://api.unify.ai/v0

# Admin key for Orchestra (needed to fetch all assistants)
ORCHESTRA_ADMIN_KEY=...
```

---

## Scheduled Token Refresh

A Cloud Scheduler job refreshes all Microsoft tokens every 30 minutes:

```bash
# Create the scheduler job
gcloud scheduler jobs create http microsoft-token-refresh \
  --schedule="*/30 * * * *" \
  --uri="https://your-adapters-url/scheduled/microsoft-tokens" \
  --http-method=POST \
  --message-body='{}' \
  --time-zone="UTC" \
  --location="us-central1"
```

### How it works

```
POST /scheduled/microsoft-tokens

1. Fetches ALL assistants in a single call (GET /admin/assistant)
2. Skips those without Microsoft tokens in secrets
3. For each with tokens:
   - Uses refresh_token to get new access_token
   - Stores updated tokens back to secrets
4. Returns: { refreshed: ["email1", ...], failed: [{email, error}, ...] }
```

### Why every 30 minutes?

- Access tokens expire in 60-90 minutes
- 30-min refresh ensures tokens are always valid with buffer
- Using the refresh token also resets its 90-day inactivity clock
- Single scheduled job - no refresh logic needed in endpoints

### How scopes are chosen per source

The scheduler dispatches refresh against one of three Azure app registrations
based on `MICROSOFT_TOKEN_SOURCE` (see `_resolve_ms_refresh_credentials` in
`adapters/main.py`). The `scope` string sent with `grant_type=refresh_token`
depends on the source:

| Source | App | Scope sent on refresh |
|--------|-----|----------------------|
| `byod` | `MS365_BYOD_*` | `MICROSOFT_GRANTED_SCOPES` (frozen at user consent time) |
| `enterprise` | per-assistant `AZURE_*` | `https://graph.microsoft.com/.default offline_access` |
| `unify_ropc` | `MS365_ADMIN_*` | `build_scope_string("microsoft", ["email", "teams"])` recomputed every tick from `common/scopes.py` |

For `unify_ropc`, this means edits to `MICROSOFT_SCOPE_BUNDLES["email"]` or
`["teams"]` in `common/scopes.py` take effect on the next scheduled refresh —
no mailbox re-provisioning required. `MICROSOFT_GRANTED_SCOPES` is ignored for
this source and is re-stamped after each successful refresh to match what the
access token actually carries.

### Required permissions on the `MS365_ADMIN_*` app (unify-managed mailboxes)

Because `unify_ropc` refresh always requests the full current `email + teams`
bundle, the `MS365_ADMIN_*` Azure app registration must have **every** scope
in `MICROSOFT_BASE_SCOPES + MICROSOFT_SCOPE_BUNDLES["email"] + MICROSOFT_SCOPE_BUNDLES["teams"]`
(from `common/scopes.py`) configured as Delegated permissions and
**admin-consented** for the Unify tenant. As of today, that's:

- Base: `User.Read`, `offline_access`
- Email: `Mail.Read`, `Mail.Send`, `Mail.ReadWrite`
- Teams: `Chat.Read`, `Chat.ReadWrite`, `ChatMessage.Read`,
  `ChannelMessage.Send`, `ChannelMessage.Read.All`, `Team.ReadBasic.All`,
  `Channel.ReadBasic.All`, `Channel.Create`, `TeamMember.Read.All`

If any scope is missing admin consent, refresh will fail with
`AADSTS65001` / `invalid_scope` and the mailbox will surface in
`results["failed"]`. Whenever a new scope is added to either bundle in
`common/scopes.py`, add + admin-consent it on the admin app **before**
deploying the change.

---

## Orchestra API Integration

### Get Assistant by Email (OAuth callback)
```
GET /admin/assistant?email={assistant_email}
→ { info: [{ agent_id, api_key, secrets: { AZURE_CLIENT_SECRET, ... } }] }
```

### Get All Assistants (scheduled token refresh)
```
GET /admin/assistant
→ { info: [{ agent_id, api_key, email, secrets: {...} }, ...] }
```

### Store Secret
```
POST /assistant/{assistant_id}/secret
Headers: Authorization: Bearer {api_key}
{
  "secret_name": "MICROSOFT_ACCESS_TOKEN",
  "secret_value": "..."
}
```

### Pre-configured secrets (during onboarding)
Must be added before OAuth flow:
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`

### Secrets stored by OAuth callback
- `MICROSOFT_ACCESS_TOKEN`
- `MICROSOFT_REFRESH_TOKEN`
- `MICROSOFT_TOKEN_EXPIRES_AT`

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

**Fix:** Ensure the scheduled token refresh job (`/scheduled/microsoft-tokens`) is running every 30 minutes. Check Cloud Scheduler logs.

### "No Microsoft access token found"
The user hasn't completed the OAuth flow yet.

**Fix:** Have the user visit the OAuth URL to authorize the integration.

### "Required permissions to access user-scoped chat message subscription ('Chat.Read, Chat.ReadWrite') are missing"
The access token doesn't have the required Teams Chat permissions.

**Cause:** Either:
- `Chat.Read` / `Chat.ReadWrite` permissions were never added to the Azure AD app
- Permissions were added after the user authorized, so they're not in the token

**Fix:**
1. Go to Azure Portal → App Registration → API Permissions
2. Ensure `Chat.Read` and `Chat.ReadWrite` (Delegated) are added
3. Click "Grant admin consent"
4. **Have the user re-authorize** by visiting the OAuth URL again

> Tokens only contain permissions that existed and were consented at authorization time. Adding permissions later requires a fresh OAuth flow.
