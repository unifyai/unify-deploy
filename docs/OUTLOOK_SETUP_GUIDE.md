# Microsoft Outlook Email Integration Setup Guide

This guide walks you through setting up Microsoft Graph API for Outlook email integration with Unify.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Step 1: Access Azure Portal](#step-1-access-azure-portal)
3. [Step 2: Register an Application](#step-2-register-an-application)
4. [Step 3: Configure API Permissions](#step-3-configure-api-permissions)
5. [Step 4: Create Client Secret](#step-4-create-client-secret)
6. [Step 5: Configure Environment Variables](#step-5-configure-environment-variables)
7. [Step 6: Set Up Webhook Endpoint](#step-6-set-up-webhook-endpoint)
8. [Step 7: Test the Integration](#step-7-test-the-integration)
9. [Troubleshooting](#troubleshooting)

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

> ### 📧 One App, Multiple Mailboxes
>
> A single app registration can access **all mailboxes** in your tenant - you don't need a separate app for each email account.
>
> | Component | Scope |
> |-----------|-------|
> | **Tenant ID** | Your organization (e.g., `tenant.onmicrosoft.com`) |
> | **Client ID** | Your app - can access any user in the tenant |
> | **Permissions** | Control what the app can do across all mailboxes |
>
> With application permissions (configured in Step 3), the app authenticates as itself and can read/send mail for any mailbox by specifying the user's email address. For example:
> - `assistant1@company.com` - Assistant A
> - `assistant2@company.com` - Assistant B
> - `support@company.com` - Assistant C
>
> All handled by the same app registration.
>
> **To restrict access** to specific mailboxes only, see [Application Access Policies](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access).

---

## Step 3: Configure API Permissions

1. In your app registration, click **"API permissions"** in the left sidebar
2. Click **"+ Add a permission"**
3. Select **"Microsoft Graph"**
4. Select **"Application permissions"** (NOT Delegated permissions)

5. Search for and add these permissions:

   | Permission | Description |
   |------------|-------------|
   | `Mail.Read` | Read mail in all mailboxes |
   | `Mail.Send` | Send mail as any user |
   | `Mail.ReadWrite` | Read and write mail in all mailboxes |
   | `User.Read.All` | Read all users' profiles (optional, for user lookup) |

6. Click **"Add permissions"**

7. **Important:** Click **"Grant admin consent for [Your Organization]"**
   - You'll see a prompt asking for confirmation
   - Click "Yes"
   - All permissions should now show a green checkmark ✅

![API Permissions](https://learn.microsoft.com/en-us/azure/active-directory/develop/media/quickstart-configure-app-access-web-apis/portal-permissions.png)

> ⚠️ **Without admin consent, the integration will not work.** The permissions will show a warning icon until consent is granted.

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
UNITY_COMMS_URL=https://your-domain.com
```

### Where to find each value:

| Variable | Where to Find |
|----------|---------------|
| `AZURE_TENANT_ID` | App registration → Overview → "Directory (tenant) ID" |
| `AZURE_CLIENT_ID` | App registration → Overview → "Application (client) ID" |
| `AZURE_CLIENT_SECRET` | The value you copied in Step 4 |
| `OUTLOOK_WEBHOOK_SECRET` | Generate a random string (e.g., `openssl rand -hex 32`) |

---

## Step 6: Set Up Webhook Endpoint

Microsoft Graph uses webhooks to notify your application about new emails. Unlike Gmail's Pub/Sub, you need a publicly accessible HTTPS endpoint.

### 6.1 Ensure Your Endpoint is Accessible

Your `/outlook/webhook` endpoint must be:
- Publicly accessible via HTTPS
- Able to respond within 3 seconds
- Return a 200-202 status code

### 6.2 Register a Subscription

Once your app is deployed, create a subscription for each mailbox you want to monitor:

```bash
curl -X POST "https://your-domain.com/outlook/watch" \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "primary_email": "user@yourdomain.com",
    "webhook_url": "https://your-domain.com/outlook/webhook"
  }'
```

### 6.3 Subscription Renewal

**Important:** Microsoft Graph subscriptions expire after **3 days** for mail resources.

Set up a scheduled job (cron, Cloud Scheduler, etc.) to renew subscriptions:

```bash
# Run every 2 days to renew before expiry
curl -X POST "https://your-domain.com/outlook/watch/renew" \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "subscription_id": "the-subscription-id-from-watch-response"
  }'
```

---

## Step 7: Test the Integration

### 7.1 Test Sending an Email

```bash
curl -X POST "https://your-domain.com/outlook/send" \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "sender@yourdomain.com",
    "to": "recipient@example.com",
    "subject": "Test from Unify",
    "body": "This is a test email sent via Microsoft Graph API."
  }'
```

### 7.2 Test Getting a Message

```bash
curl "https://your-domain.com/outlook/message?user_email=user@yourdomain.com&message_id=MESSAGE_ID" \
  -H "Authorization: Bearer YOUR_ADMIN_KEY"
```

### 7.3 Test Webhook (Manual)

Send a test email to a monitored mailbox and check your application logs for the webhook notification.

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
