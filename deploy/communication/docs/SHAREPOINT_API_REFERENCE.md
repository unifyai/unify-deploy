# SharePoint API Reference

This document describes all available SharePoint/OneDrive API endpoints for file and document management.

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Key Concepts](#key-concepts)
4. [Endpoints](#endpoints)
   - [Sites](#sites)
   - [Drives](#drives)
   - [Files & Folders](#files--folders)
   - [Search](#search)
5. [Examples](#examples)
6. [Error Handling](#error-handling)

---

## Overview

These endpoints provide programmatic access to SharePoint sites and OneDrive through Microsoft Graph API. You can:

- Browse SharePoint sites and document libraries
- List, upload, download, and delete files
- Create folders and organize documents
- Search across drives

**Base URL:** `{UNITY_COMMS_URL}/sharepoint`

---

## Authentication

All endpoints require admin authentication via the `Authorization` header:

```
Authorization: Bearer {ORCHESTRA_ADMIN_KEY}
```

Additionally, each request requires a `user_email` query parameter to identify whose Microsoft account to use for the operation.

---

## Key Concepts

### SharePoint vs OneDrive

| Term | Description | API Reference |
|------|-------------|---------------|
| **OneDrive** | Personal file storage for each user | `drive_id = "me"` |
| **SharePoint Site** | Team workspace with shared documents | `/sites/{site_id}` |
| **Drive** | A document library (called "Drive" in API) | `/drives/{drive_id}` |
| **DriveItem** | A file or folder within a drive | `/drives/{drive_id}/items/{item_id}` |

### When to Use What

| Use Case | Endpoint Pattern |
|----------|------------------|
| User's personal files | `drive_id = "me"` |
| Team shared documents | Get site → Get drive → Use `drive_id` |
| Cross-team documents | Use specific `site_id` and `drive_id` |

---

## Endpoints

### Sites

#### List SharePoint Sites

```
GET /sharepoint/sites
```

List all SharePoint sites the user has access to.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |
| `search` | string | No | Filter sites by name |

**Response:**

```json
{
  "sites": [
    {
      "id": "contoso.sharepoint.com,abc123,def456",
      "name": "Sales Team",
      "web_url": "https://contoso.sharepoint.com/sites/sales",
      "description": "Sales department documents"
    }
  ]
}
```

**Example:**

```bash
curl "${BASE_URL}/sharepoint/sites?user_email=user@company.com&search=Sales" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

#### Get Site Details

```
GET /sharepoint/sites/{site_id}
```

Get details of a specific SharePoint site.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `site_id` | string | Yes | The SharePoint site ID |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

```json
{
  "id": "contoso.sharepoint.com,abc123,def456",
  "name": "Sales Team",
  "web_url": "https://contoso.sharepoint.com/sites/sales",
  "description": "Sales department documents"
}
```

---

### Drives

#### List User's Drives

```
GET /sharepoint/drives
```

List the user's personal OneDrive and any other accessible drives.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

```json
{
  "drives": [
    {
      "id": "b!abc123...",
      "name": "OneDrive",
      "drive_type": "personal",
      "web_url": "https://contoso-my.sharepoint.com/personal/user_contoso_com",
      "is_personal": true
    },
    {
      "id": "b!def456...",
      "name": "Documents",
      "drive_type": "documentLibrary",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents",
      "is_personal": false
    }
  ]
}
```

**Example:**

```bash
curl "${BASE_URL}/sharepoint/drives?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

#### List Site's Drives

```
GET /sharepoint/sites/{site_id}/drives
```

List all document libraries (drives) in a SharePoint site.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `site_id` | string | Yes | The SharePoint site ID |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

```json
{
  "drives": [
    {
      "id": "b!abc123...",
      "name": "Documents",
      "drive_type": "documentLibrary",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents"
    },
    {
      "id": "b!def456...",
      "name": "Contracts",
      "drive_type": "documentLibrary",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Contracts"
    }
  ]
}
```

---

### Files & Folders

#### List Items in a Drive

```
GET /sharepoint/drives/{drive_id}/items
```

List files and folders in a drive or folder.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |
| `path` | string | No | Folder path (e.g., `"Documents/Projects"`) |
| `item_id` | string | No | Folder item ID (alternative to path) |

> **Note:** If neither `path` nor `item_id` is provided, lists the root folder.

**Response:**

```json
{
  "items": [
    {
      "id": "01ABC123...",
      "name": "Q4 Report.xlsx",
      "type": "file",
      "size": 45678,
      "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "created": "2024-01-15T10:30:00Z",
      "modified": "2024-01-20T14:22:00Z",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Q4%20Report.xlsx"
    },
    {
      "id": "01DEF456...",
      "name": "Archive",
      "type": "folder",
      "size": 0,
      "mime_type": null,
      "created": "2024-01-10T09:00:00Z",
      "modified": "2024-01-10T09:00:00Z",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Archive"
    }
  ]
}
```

**Examples:**

```bash
# List root of personal OneDrive
curl "${BASE_URL}/sharepoint/drives/me/items?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"

# List a specific folder by path
curl "${BASE_URL}/sharepoint/drives/${DRIVE_ID}/items?user_email=user@company.com&path=Projects/2024" \
  -H "Authorization: Bearer ${API_KEY}"

# List a specific folder by item ID
curl "${BASE_URL}/sharepoint/drives/${DRIVE_ID}/items?user_email=user@company.com&item_id=01ABC123" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

#### Get Item Metadata

```
GET /sharepoint/drives/{drive_id}/items/{item_id}
```

Get detailed metadata for a specific file or folder.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |
| `item_id` | string | Yes | The file or folder ID |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

```json
{
  "id": "01ABC123...",
  "name": "Q4 Report.xlsx",
  "type": "file",
  "size": 45678,
  "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-20T14:22:00Z",
  "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Q4%20Report.xlsx",
  "parent_path": "/drives/b!abc123/root:/Documents"
}
```

---

#### Download File

```
GET /sharepoint/drives/{drive_id}/items/{item_id}/content
```

Download a file's content.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |
| `item_id` | string | Yes | The file ID |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

Returns the raw file content with appropriate `Content-Type` and `Content-Disposition` headers.

**Example:**

```bash
curl "${BASE_URL}/sharepoint/drives/me/items/${ITEM_ID}/content?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -o downloaded_file.xlsx
```

---

#### Upload File

```
PUT /sharepoint/drives/{drive_id}/upload
```

Upload a file to a drive. Parent folders are created automatically if they don't exist.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Request Body:**

```json
{
  "path": "folder/subfolder/filename.txt",
  "content": "File content or base64 encoded data"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | Yes | Full path including filename |
| `content` | string | Yes | Plain text or base64-encoded content |

> **Note:** For files larger than 4MB, chunked upload should be used (not implemented in this version).

**Response:**

```json
{
  "success": true,
  "id": "01ABC123...",
  "name": "filename.txt",
  "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/folder/subfolder/filename.txt",
  "size": 1234
}
```

**Examples:**

```bash
# Upload plain text file
curl -X PUT "${BASE_URL}/sharepoint/drives/me/upload?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "Reports/quarterly.txt",
    "content": "Q4 2024 Report\n\nSales increased by 15%..."
  }'

# Upload to SharePoint drive
curl -X PUT "${BASE_URL}/sharepoint/drives/${DRIVE_ID}/upload?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "Contracts/2024/client-agreement.pdf",
    "content": "JVBERi0xLjQKJ..."
  }'
```

---

#### Create Folder

```
POST /sharepoint/drives/{drive_id}/folder
```

Create a new folder.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Request Body:**

```json
{
  "name": "New Folder",
  "parent_path": "Documents/Projects"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Name of the new folder |
| `parent_path` | string | No | Parent folder path (defaults to root) |

**Response:**

```json
{
  "success": true,
  "id": "01ABC123...",
  "name": "New Folder",
  "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Projects/New%20Folder"
}
```

**Example:**

```bash
curl -X POST "${BASE_URL}/sharepoint/drives/me/folder?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "2024 Archive",
    "parent_path": "Documents"
  }'
```

---

#### Delete Item

```
DELETE /sharepoint/drives/{drive_id}/items/{item_id}
```

Delete a file or folder (including all contents if folder).

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |
| `item_id` | string | Yes | The file or folder ID to delete |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |

**Response:**

```json
{
  "success": true
}
```

**Example:**

```bash
curl -X DELETE "${BASE_URL}/sharepoint/drives/me/items/${ITEM_ID}?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

### Search

#### Search Files

```
GET /sharepoint/drives/{drive_id}/search
```

Search for files within a drive.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drive_id` | string | Yes | The drive ID. Use `"me"` for personal OneDrive |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_email` | string | Yes | User's email for token lookup |
| `q` | string | Yes | Search query |

**Response:**

```json
{
  "results": [
    {
      "id": "01ABC123...",
      "name": "Q4 Sales Report.xlsx",
      "type": "file",
      "path": "/drives/b!abc123/root:/Documents/Reports",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Reports/Q4%20Sales%20Report.xlsx"
    },
    {
      "id": "01DEF456...",
      "name": "Sales Forecast.docx",
      "type": "file",
      "path": "/drives/b!abc123/root:/Documents",
      "web_url": "https://contoso.sharepoint.com/sites/sales/Documents/Sales%20Forecast.docx"
    }
  ]
}
```

**Example:**

```bash
curl "${BASE_URL}/sharepoint/drives/me/search?user_email=user@company.com&q=quarterly%20report" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

## Examples

### Complete Workflow: Upload to SharePoint

```bash
# 1. List available SharePoint sites
curl "${BASE_URL}/sharepoint/sites?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"

# Response includes site_id: "contoso.sharepoint.com,abc123,def456"

# 2. Get drives in that site
curl "${BASE_URL}/sharepoint/sites/contoso.sharepoint.com,abc123,def456/drives?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"

# Response includes drive_id: "b!xyz789..."

# 3. Create a folder
curl -X POST "${BASE_URL}/sharepoint/drives/b!xyz789.../folder?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"name": "Q1 2024", "parent_path": "Reports"}'

# 4. Upload a file
curl -X PUT "${BASE_URL}/sharepoint/drives/b!xyz789.../upload?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "Reports/Q1 2024/summary.txt",
    "content": "Q1 2024 Summary\n\nRevenue: $1.2M\nGrowth: 12%"
  }'
```

### Working with Personal OneDrive

```bash
# List files in OneDrive root
curl "${BASE_URL}/sharepoint/drives/me/items?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}"

# Upload to OneDrive
curl -X PUT "${BASE_URL}/sharepoint/drives/me/upload?user_email=user@company.com" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"path": "notes.txt", "content": "My personal notes..."}'

# Search OneDrive
curl "${BASE_URL}/sharepoint/drives/me/search?user_email=user@company.com&q=invoice" \
  -H "Authorization: Bearer ${API_KEY}"
```

---

## Error Handling

All endpoints return standard HTTP status codes:

| Status Code | Description |
|-------------|-------------|
| `200` | Success |
| `400` | Bad request (missing required fields) |
| `401` | Unauthorized (invalid or missing API key) |
| `403` | Forbidden (user doesn't have access) |
| `404` | Not found (site, drive, or item doesn't exist) |
| `500` | Internal server error |

**Error Response Format:**

```json
{
  "detail": "Error message describing what went wrong"
}
```

**Common Errors:**

| Error | Cause | Solution |
|-------|-------|----------|
| `"Missing required fields: path, content"` | Upload request missing data | Include both `path` and `content` in request body |
| `"Cannot download a folder"` | Attempted to download a folder | Use list items to browse folder contents |
| `"Access denied"` | User lacks permissions | Ensure user has access to the site/drive |
| `"Item not found"` | Invalid item_id | Verify the item ID exists |

---

## Rate Limits

Microsoft Graph API has rate limits. If you receive `429 Too Many Requests`, implement exponential backoff and retry.

---

## Related Documentation

- [Microsoft Graph Files API](https://learn.microsoft.com/en-us/graph/api/resources/driveitem)
- [SharePoint Sites API](https://learn.microsoft.com/en-us/graph/api/resources/site)
- [OneDrive API](https://learn.microsoft.com/en-us/graph/api/resources/onedrive)
