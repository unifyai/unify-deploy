# SharePoint & OneDrive Integration Setup Guide

This guide walks you through setting up Microsoft Graph API for SharePoint and OneDrive file operations.

## Table of Contents
1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Step 1: Extend Azure AD App Permissions](#step-1-extend-azure-ad-app-permissions)
4. [Step 2: Configure Environment Variables](#step-2-configure-environment-variables)
5. [Step 3: API Endpoints Reference](#step-3-api-endpoints-reference)
6. [Step 4: Common Operations](#step-4-common-operations)
7. [Step 5: Webhooks for Change Notifications](#step-5-webhooks-for-change-notifications)
8. [Troubleshooting](#troubleshooting)

---

## Overview

### What You Can Do

With Microsoft Graph API for SharePoint/OneDrive, you can:

- **List** sites, drives, folders, and files
- **Read** file contents and metadata
- **Upload** new files
- **Update** existing files
- **Delete** files and folders
- **Create** folders
- **Share** files with users
- **Search** across all drives
- **Watch** for changes via webhooks

### Architecture

```
Your Application → Microsoft Graph API → SharePoint / OneDrive
                                      ↓
                              User's Files & Shared Drives
```

### Terminology

| Term | Description |
|------|-------------|
| **Site** | A SharePoint site (team site, communication site, etc.) |
| **Drive** | A document library within a site, or a user's OneDrive |
| **DriveItem** | A file or folder within a drive |
| **Shared Drive** | A drive accessible by multiple users (SharePoint document library) |

---

## Prerequisites

- [ ] **Microsoft 365 account** with SharePoint access
- [ ] **Azure AD App Registration** (reuse from Outlook/Teams setup)
- [ ] **User OAuth flow** configured for delegated permissions

> **Note:** If you already set up the Outlook or Teams integration, you can add SharePoint permissions to the same app registration. We use **delegated permissions** (user context), not application permissions.

---

## Step 1: Extend Azure AD App Permissions

### 1.1 Access Your App Registration

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** → **App registrations**
3. Select your existing app (e.g., "Unify Email Integration") or create a new one

### 1.2 Add SharePoint/OneDrive Permissions (Delegated)

1. Click **API permissions** in the left sidebar
2. Click **+ Add a permission**
3. Select **Microsoft Graph**
4. Select **Delegated permissions** (NOT Application permissions)

Add these permissions based on your needs:

#### For Reading Files (Minimum)

| Permission | Type | Description |
|------------|------|-------------|
| `Sites.Read.All` | Delegated | Read SharePoint sites user has access to |
| `Files.Read.All` | Delegated | Read files user has access to |

#### For Full File Management (Recommended)

| Permission | Type | Description |
|------------|------|-------------|
| `Sites.ReadWrite.All` | Delegated | Read/write SharePoint sites user has access to |
| `Files.ReadWrite.All` | Delegated | Read/write files user has access to |

#### For Managing Sites

| Permission | Type | Description |
|------------|------|-------------|
| `Sites.Manage.All` | Delegated | Create, edit, delete lists in sites user can access |

#### Required for Token Refresh

| Permission | Type | Description |
|------------|------|-------------|
| `offline_access` | Delegated | Obtain refresh tokens (you likely already have this) |

### 1.3 Admin Consent (If Required)

For delegated permissions, admin consent is often **not required** — users can consent themselves. However, some permissions may require admin consent depending on your tenant settings.

**If admin consent is needed:**
1. Click **Grant admin consent for [Organization]**
2. Confirm by clicking **Yes**
3. All permissions should show green checkmarks ✅

**Delegated vs Application Permissions:**

| Aspect | Delegated (What We Use) | Application |
|--------|-------------------------|-------------|
| **Scope** | Only files the *user* can access | All files in the org |
| **Auth** | User signs in via OAuth | Client credentials only |
| **Use case** | Acting on behalf of a user | Background services |

---

## Step 2: Configure Environment Variables

If you already have Outlook/Teams configured, you can reuse the same credentials:

```bash
# Microsoft Graph API Configuration (same as Outlook/Teams)
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret

# Optional: Default SharePoint site
SHAREPOINT_SITE_ID=your-default-site-id
SHAREPOINT_DRIVE_ID=your-default-drive-id
```

### OAuth Scopes

When requesting tokens, include the SharePoint scopes in your auth request:

```
scope=https://graph.microsoft.com/Sites.ReadWrite.All 
      https://graph.microsoft.com/Files.ReadWrite.All
      offline_access
      ... your existing scopes ...
```

Or if using `/.default`, the new permissions are automatically included once added to the app registration.

### Finding Site and Drive IDs

You can find these via the Graph API or Graph Explorer:

**Site ID:**
```
GET https://graph.microsoft.com/v1.0/sites/{hostname}:/{site-path}

Example:
GET https://graph.microsoft.com/v1.0/sites/yourdomain.sharepoint.com:/sites/YourSiteName
```

**Drive ID:**
```
GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives
```

---

## Step 3: API Endpoints Reference

Here are the endpoints you can implement (similar to the Outlook views):

### Sites

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/sites` | GET | List all sites user has access to |
| `/sharepoint/sites/{site_id}` | GET | Get specific site details |
| `/sharepoint/sites/search` | GET | Search sites by name |

### Drives

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/drives` | GET | List drives in a site |
| `/sharepoint/drives/{drive_id}` | GET | Get drive details |
| `/sharepoint/drives/{drive_id}/root` | GET | Get root folder contents |

### Files & Folders

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/files` | GET | List files in a folder |
| `/sharepoint/files/{item_id}` | GET | Get file metadata |
| `/sharepoint/files/{item_id}/content` | GET | Download file content |
| `/sharepoint/files/upload` | POST | Upload a file |
| `/sharepoint/files/{item_id}` | DELETE | Delete a file |
| `/sharepoint/folders` | POST | Create a folder |

### Search

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/search` | GET | Search files across drives |

### Sharing

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/files/{item_id}/share` | POST | Create sharing link |
| `/sharepoint/files/{item_id}/permissions` | GET | List permissions |

### Webhooks

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sharepoint/watch` | POST | Subscribe to drive changes |
| `/sharepoint/watch/renew` | POST | Renew subscription |
| `/sharepoint/webhook` | POST | Receive change notifications |

---

## Step 4: Common Operations

> **Note:** All examples below use delegated permissions. The `graph_client` is obtained from a user's stored access token via `get_graph_client(user_email)`.

### 4.1 List All SharePoint Sites

```python
from msgraph import GraphServiceClient

async def list_sites(graph_client: GraphServiceClient):
    """List SharePoint sites the user has access to."""
    sites = await graph_client.sites.get()
    return [
        {
            "id": site.id,
            "name": site.display_name,
            "web_url": site.web_url,
        }
        for site in (sites.value or [])
    ]
```

### 4.2 List Drives in a Site

```python
async def list_drives(graph_client: GraphServiceClient, site_id: str):
    """List all document libraries (drives) in a SharePoint site."""
    drives = await graph_client.sites.by_site_id(site_id).drives.get()
    return [
        {
            "id": drive.id,
            "name": drive.name,
            "drive_type": drive.drive_type,
            "web_url": drive.web_url,
        }
        for drive in drives.value
    ]
```

### 4.3 List Files in a Folder

```python
async def list_files(
    graph_client: GraphServiceClient,
    drive_id: str,
    folder_path: str = "root",
):
    """List files and folders in a specific path."""
    if folder_path == "root":
        items = await graph_client.drives.by_drive_id(drive_id).root.children.get()
    else:
        items = await graph_client.drives.by_drive_id(
            drive_id
        ).root.item_with_path(folder_path).children.get()
    
    return [
        {
            "id": item.id,
            "name": item.name,
            "type": "folder" if item.folder else "file",
            "size": item.size,
            "modified": item.last_modified_date_time.isoformat() if item.last_modified_date_time else None,
            "web_url": item.web_url,
        }
        for item in items.value
    ]
```

### 4.4 Download a File

```python
async def download_file(
    graph_client: GraphServiceClient,
    drive_id: str,
    item_id: str,
):
    """Download file content."""
    content = await graph_client.drives.by_drive_id(
        drive_id
    ).items.by_drive_item_id(item_id).content.get()
    
    return content  # Returns bytes
```

### 4.5 Upload a File

```python
async def upload_file(
    graph_client: GraphServiceClient,
    drive_id: str,
    folder_path: str,
    filename: str,
    content: bytes,
):
    """Upload a file to SharePoint."""
    # For files < 4MB, use simple upload
    if len(content) < 4 * 1024 * 1024:
        result = await graph_client.drives.by_drive_id(
            drive_id
        ).root.item_with_path(f"{folder_path}/{filename}").content.put(content)
        return result
    
    # For larger files, use upload session
    # See: https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession
    pass
```

### 4.6 Create a Folder

```python
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.folder import Folder

async def create_folder(
    graph_client: GraphServiceClient,
    drive_id: str,
    parent_path: str,
    folder_name: str,
):
    """Create a new folder."""
    new_folder = DriveItem(
        name=folder_name,
        folder=Folder(),
    )
    
    result = await graph_client.drives.by_drive_id(
        drive_id
    ).root.item_with_path(parent_path).children.post(new_folder)
    
    return {
        "id": result.id,
        "name": result.name,
        "web_url": result.web_url,
    }
```

### 4.7 Delete a File or Folder

```python
async def delete_item(
    graph_client: GraphServiceClient,
    drive_id: str,
    item_id: str,
):
    """Delete a file or folder."""
    await graph_client.drives.by_drive_id(
        drive_id
    ).items.by_drive_item_id(item_id).delete()
    
    return {"success": True}
```

### 4.8 Search Files

```python
async def search_files(
    graph_client: GraphServiceClient,
    drive_id: str,
    query: str,
):
    """Search for files in a drive."""
    results = await graph_client.drives.by_drive_id(
        drive_id
    ).root.search_with_q(query).get()
    
    return [
        {
            "id": item.id,
            "name": item.name,
            "path": item.parent_reference.path if item.parent_reference else None,
            "web_url": item.web_url,
        }
        for item in results.value
    ]
```

### 4.9 Create Sharing Link

```python
from msgraph.generated.drives.item.items.item.create_link.create_link_post_request_body import (
    CreateLinkPostRequestBody,
)

async def create_sharing_link(
    graph_client: GraphServiceClient,
    drive_id: str,
    item_id: str,
    link_type: str = "view",  # "view", "edit", or "embed"
    scope: str = "anonymous",  # "anonymous" or "organization"
):
    """Create a sharing link for a file."""
    request_body = CreateLinkPostRequestBody(
        type=link_type,
        scope=scope,
    )
    
    result = await graph_client.drives.by_drive_id(
        drive_id
    ).items.by_drive_item_id(item_id).create_link.post(request_body)
    
    return {
        "link": result.link.web_url,
        "type": result.link.type,
        "scope": result.link.scope,
    }
```

### 4.10 Get User's OneDrive

```python
async def get_user_onedrive(
    graph_client: GraphServiceClient,
    user_email: str,
):
    """Get a specific user's OneDrive."""
    drive = await graph_client.users.by_user_id(user_email).drive.get()
    
    return {
        "id": drive.id,
        "name": drive.name,
        "owner": drive.owner.user.display_name if drive.owner else None,
        "quota_used": drive.quota.used if drive.quota else None,
        "quota_total": drive.quota.total if drive.quota else None,
    }
```

---

## Step 5: Webhooks for Change Notifications

You can subscribe to changes in a drive (new files, updates, deletes).

### 5.1 Create Subscription

```python
from msgraph.generated.models.subscription import Subscription
from datetime import datetime, timedelta

async def watch_drive(
    graph_client: GraphServiceClient,
    drive_id: str,
    webhook_url: str,
):
    """Subscribe to changes in a drive."""
    # SharePoint/OneDrive subscriptions expire in max 30 days
    expiration = datetime.utcnow() + timedelta(days=30)
    
    subscription = Subscription(
        change_type="updated",  # "created", "updated", "deleted", or combined
        notification_url=webhook_url,
        resource=f"/drives/{drive_id}/root",
        expiration_date_time=expiration,
        client_state="your-secret-state",
    )
    
    result = await graph_client.subscriptions.post(subscription)
    
    return {
        "subscription_id": result.id,
        "expiration": result.expiration_date_time.isoformat(),
    }
```

### 5.2 Handle Webhook Notifications

```python
@router.post("/sharepoint/webhook")
async def sharepoint_webhook(request: Request):
    """Handle SharePoint change notifications."""
    # Validation request
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain")
    
    # Process notifications
    data = await request.json()
    
    for notification in data.get("value", []):
        if notification.get("clientState") != "your-secret-state":
            continue
        
        resource = notification.get("resource")
        change_type = notification.get("changeType")
        
        print(f"SharePoint change: {change_type} on {resource}")
        
        # Use delta query to get actual changes
        # See: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
    
    return Response(status_code=202)
```

### 5.3 Delta Query for Changes

```python
async def get_drive_changes(
    graph_client: GraphServiceClient,
    drive_id: str,
    delta_link: str = None,
):
    """Get changes since last sync using delta query."""
    if delta_link:
        # Use the delta link from previous call
        # You'll need to make a raw request with the delta_link
        pass
    else:
        # Initial sync - get all items
        delta = await graph_client.drives.by_drive_id(drive_id).root.delta.get()
    
    changes = []
    for item in delta.value:
        changes.append({
            "id": item.id,
            "name": item.name,
            "deleted": item.deleted is not None,
            "modified": item.last_modified_date_time,
        })
    
    # Store delta_link for next call
    next_delta_link = delta.odata_delta_link
    
    return {
        "changes": changes,
        "delta_link": next_delta_link,
    }
```

---

## Troubleshooting

### "Access denied" or "Insufficient privileges"

**Cause:** Missing API permissions, user hasn't consented, or user doesn't have access to the resource.

**Fix:**
1. Go to Azure Portal → App registrations → Your app → API permissions
2. Ensure **delegated** permissions are added (Sites.ReadWrite.All, Files.ReadWrite.All)
3. If admin consent is required for your tenant, click "Grant admin consent"
4. Have the user re-authenticate to get a fresh token with the new scopes
5. Verify the user actually has access to the SharePoint site in question

### "Site not found"

**Cause:** Incorrect site ID or path.

**Fix:**
1. Use Graph Explorer to find correct site:
   ```
   GET https://graph.microsoft.com/v1.0/sites?search=*
   ```
2. Or use the site path format:
   ```
   GET https://graph.microsoft.com/v1.0/sites/{tenant}.sharepoint.com:/sites/{sitename}
   ```

### "Item not found" when accessing files

**Cause:** Incorrect drive ID or item ID.

**Fix:**
1. List drives first to get correct drive ID
2. List items to get correct item IDs
3. Item IDs are unique within a drive, not globally

### Upload fails for large files

**Cause:** Simple upload only works for files < 4MB.

**Fix:**
Use upload session for larger files:
```python
# Create upload session
session = await graph_client.drives.by_drive_id(drive_id).root.item_with_path(
    f"{path}/{filename}"
).create_upload_session.post(CreateUploadSessionPostRequestBody())

# Upload in chunks (5-10 MB each)
upload_url = session.upload_url
# ... chunk upload logic
```

### Webhook not receiving notifications

**Cause:** Endpoint not accessible or validation failing.

**Fix:**
1. Ensure endpoint is publicly accessible via HTTPS
2. Return `validationToken` as plain text during subscription creation
3. Check that `clientState` matches in notifications
4. Verify subscription hasn't expired (max 30 days for drives)

---

## Full Example: SharePoint Views

Here's a complete example of SharePoint endpoints using **delegated permissions** (same pattern as `outlook_views.py`):

```python
# communication/sharepoint/views.py

import logging
from fastapi import APIRouter, HTTPException, Request, Response
from typing import Optional

from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.folder import Folder

from communication.helpers import get_graph_client  # Uses delegated tokens

router = APIRouter()


@router.get("/sites")
async def list_sites(user_email: str, search: Optional[str] = None):
    """List SharePoint sites the user has access to."""
    graph = await get_graph_client(user_email)  # Gets token for this user
    
    if search:
        sites = await graph.sites.get(
            request_configuration=lambda c: setattr(c.query_parameters, "search", search)
        )
    else:
        sites = await graph.sites.get()
    
    return {
        "sites": [
            {"id": s.id, "name": s.display_name, "url": s.web_url}
            for s in (sites.value or [])
        ]
    }


@router.get("/drives")
async def list_user_drives(user_email: str):
    """List user's OneDrive and accessible drives."""
    graph = await get_graph_client(user_email)
    
    # Get user's personal OneDrive
    my_drive = await graph.me.drive.get()
    
    return {
        "drives": [{
            "id": my_drive.id,
            "name": my_drive.name or "OneDrive",
            "drive_type": my_drive.drive_type,
            "web_url": my_drive.web_url,
        }]
    }


@router.get("/sites/{site_id}/drives")
async def list_site_drives(user_email: str, site_id: str):
    """List drives (document libraries) in a SharePoint site."""
    graph = await get_graph_client(user_email)
    drives = await graph.sites.by_site_id(site_id).drives.get()
    
    return {
        "drives": [
            {"id": d.id, "name": d.name, "type": d.drive_type}
            for d in (drives.value or [])
        ]
    }


@router.get("/drives/{drive_id}/items")
async def list_items(user_email: str, drive_id: str, path: Optional[str] = None):
    """List files in a drive folder."""
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        drive_ref = graph.me.drive
    else:
        drive_ref = graph.drives.by_drive_id(drive_id)
    
    if path:
        items = await drive_ref.root.item_with_path(path).children.get()
    else:
        items = await drive_ref.root.children.get()
    
    return {
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "type": "folder" if i.folder else "file",
                "size": i.size,
                "url": i.web_url,
            }
            for i in (items.value or [])
        ]
    }


@router.get("/drives/{drive_id}/items/{item_id}/content")
async def download_file(user_email: str, drive_id: str, item_id: str):
    """Download file content."""
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        drive_ref = graph.me.drive
    else:
        drive_ref = graph.drives.by_drive_id(drive_id)
    
    item = await drive_ref.items.by_drive_item_id(item_id).get()
    content = await drive_ref.items.by_drive_item_id(item_id).content.get()
    
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={item.name}"}
    )


@router.put("/drives/{drive_id}/upload")
async def upload_file(request: Request, user_email: str, drive_id: str):
    """Upload a file."""
    data = await request.json()
    path = data.get("path")
    content = data.get("content")
    
    if not path or content is None:
        raise HTTPException(status_code=400, detail="Missing path or content")
    
    import base64
    try:
        file_bytes = base64.b64decode(content)
    except Exception:
        file_bytes = content.encode("utf-8")
    
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        drive_ref = graph.me.drive
    else:
        drive_ref = graph.drives.by_drive_id(drive_id)
    
    result = await drive_ref.root.item_with_path(path).content.put(file_bytes)
    
    return {"success": True, "id": result.id, "web_url": result.web_url}


@router.delete("/drives/{drive_id}/items/{item_id}")
async def delete_item(user_email: str, drive_id: str, item_id: str):
    """Delete a file or folder."""
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        await graph.me.drive.items.by_drive_item_id(item_id).delete()
    else:
        await graph.drives.by_drive_id(drive_id).items.by_drive_item_id(item_id).delete()
    
    return {"success": True}


@router.post("/drives/{drive_id}/folder")
async def create_folder(request: Request, user_email: str, drive_id: str):
    """Create a folder."""
    data = await request.json()
    folder_name = data.get("name")
    parent_path = data.get("parent_path")
    
    if not folder_name:
        raise HTTPException(status_code=400, detail="Missing folder name")
    
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        drive_ref = graph.me.drive
    else:
        drive_ref = graph.drives.by_drive_id(drive_id)
    
    new_folder = DriveItem(name=folder_name, folder=Folder())
    
    if parent_path:
        result = await drive_ref.root.item_with_path(parent_path).children.post(new_folder)
    else:
        result = await drive_ref.root.children.post(new_folder)
    
    return {"success": True, "id": result.id, "web_url": result.web_url}


@router.get("/drives/{drive_id}/search")
async def search_files(user_email: str, drive_id: str, q: str):
    """Search files in a drive."""
    graph = await get_graph_client(user_email)
    
    if drive_id == "me":
        results = await graph.me.drive.root.search_with_q(q).get()
    else:
        results = await graph.drives.by_drive_id(drive_id).root.search_with_q(q).get()
    
    return {
        "results": [
            {"id": i.id, "name": i.name, "web_url": i.web_url}
            for i in (results.value or [])
        ]
    }
```

> **Key Difference from Application Permissions:** Each endpoint requires a `user_email` parameter to identify whose token to use. The `get_graph_client()` helper retrieves the stored access token for that user and creates a Graph client with delegated permissions.

---

## Useful Links

- [Microsoft Graph Files API](https://learn.microsoft.com/en-us/graph/api/resources/driveitem)
- [SharePoint Sites API](https://learn.microsoft.com/en-us/graph/api/resources/site)
- [OneDrive API](https://learn.microsoft.com/en-us/graph/api/resources/onedrive)
- [Delta Query for Sync](https://learn.microsoft.com/en-us/graph/api/driveitem-delta)
- [Upload Large Files](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession)
- [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer)

---

## Next Steps

1. **Add delegated permissions** to your existing Azure AD app (Sites.ReadWrite.All, Files.ReadWrite.All)
2. **Have users re-authenticate** to get tokens with the new scopes
3. **Test with Graph Explorer** using delegated permissions before coding
4. **Implement endpoints** based on your needs (see Full Example above)
5. **Set up webhooks** if you need real-time notifications

For related guides:
- [Outlook Email Setup](./OUTLOOK_SETUP_GUIDE.md)
- [Teams + LiveKit Setup](./TEAMS_LIVEKIT_SETUP_GUIDE.md)
- [SharePoint API Reference](./SHAREPOINT_API_REFERENCE.md)
