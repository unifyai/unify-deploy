import logging
from fastapi import APIRouter, HTTPException, Request, Response
from typing import Optional

from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.folder import Folder

from communication.helpers import get_graph_client

router = APIRouter()


# =============================================================================
# Sites
# =============================================================================


@router.get("/sites")
async def list_sites(user_email: str, search: Optional[str] = None):
    """
    List SharePoint sites the user has access to.

    Query params:
        user_email: The user's email (for token lookup)
        search: Optional search term to filter sites
    """
    try:
        graph = await get_graph_client(user_email)

        if search:
            sites = await graph.sites.get(
                request_configuration=lambda c: setattr(
                    c.query_parameters, "search", search
                )
            )
        else:
            # Get sites the user is following or has access to
            sites = await graph.sites.get()

        return {
            "sites": [
                {
                    "id": site.id,
                    "name": site.display_name,
                    "web_url": site.web_url,
                    "description": site.description,
                }
                for site in (sites.value or [])
            ]
        }

    except Exception as e:
        logging.error(f"Failed to list sites: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sites/{site_id}")
async def get_site(user_email: str, site_id: str):
    """
    Get details of a specific SharePoint site.

    Path params:
        site_id: The site ID

    Query params:
        user_email: The user's email (for token lookup)
    """
    try:
        graph = await get_graph_client(user_email)
        site = await graph.sites.by_site_id(site_id).get()

        return {
            "id": site.id,
            "name": site.display_name,
            "web_url": site.web_url,
            "description": site.description,
        }

    except Exception as e:
        logging.error(f"Failed to get site: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Drives (Document Libraries)
# =============================================================================


@router.get("/drives")
async def list_user_drives(user_email: str):
    """
    List user's OneDrive and accessible drives.

    Query params:
        user_email: The user's email (for token lookup)
    """
    try:
        graph = await get_graph_client(user_email)

        # Get user's personal OneDrive
        my_drive = await graph.me.drive.get()

        # Get all drives accessible to the user
        drives = await graph.me.drives.get()

        all_drives = []
        if my_drive:
            all_drives.append(
                {
                    "id": my_drive.id,
                    "name": my_drive.name or "OneDrive",
                    "drive_type": my_drive.drive_type,
                    "web_url": my_drive.web_url,
                    "is_personal": True,
                }
            )

        for drive in drives.value or []:
            if drive.id != my_drive.id:
                all_drives.append(
                    {
                        "id": drive.id,
                        "name": drive.name,
                        "drive_type": drive.drive_type,
                        "web_url": drive.web_url,
                        "is_personal": False,
                    }
                )

        return {"drives": all_drives}

    except Exception as e:
        logging.error(f"Failed to list drives: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sites/{site_id}/drives")
async def list_site_drives(user_email: str, site_id: str):
    """
    List drives (document libraries) in a SharePoint site.

    Path params:
        site_id: The site ID

    Query params:
        user_email: The user's email (for token lookup)
    """
    try:
        graph = await get_graph_client(user_email)
        drives = await graph.sites.by_site_id(site_id).drives.get()

        return {
            "drives": [
                {
                    "id": drive.id,
                    "name": drive.name,
                    "drive_type": drive.drive_type,
                    "web_url": drive.web_url,
                }
                for drive in (drives.value or [])
            ]
        }

    except Exception as e:
        logging.error(f"Failed to list site drives: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Files & Folders
# =============================================================================


@router.get("/drives/{drive_id}/items")
async def list_items(
    user_email: str,
    drive_id: str,
    path: Optional[str] = None,
    item_id: Optional[str] = None,
):
    """
    List files and folders in a drive.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)

    Query params:
        user_email: The user's email (for token lookup)
        path: Optional folder path (e.g., "Documents/Projects")
        item_id: Optional folder item ID (alternative to path)
    """
    try:
        graph = await get_graph_client(user_email)

        if drive_id == "me":
            drive_ref = graph.me.drive
        else:
            drive_ref = graph.drives.by_drive_id(drive_id)

        if item_id:
            items = await drive_ref.items.by_drive_item_id(item_id).children.get()
        elif path:
            items = await drive_ref.root.item_with_path(path).children.get()
        else:
            items = await drive_ref.root.children.get()

        return {
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "type": "folder" if item.folder else "file",
                    "size": item.size,
                    "mime_type": item.file.mime_type if item.file else None,
                    "created": (
                        item.created_date_time.isoformat()
                        if item.created_date_time
                        else None
                    ),
                    "modified": (
                        item.last_modified_date_time.isoformat()
                        if item.last_modified_date_time
                        else None
                    ),
                    "web_url": item.web_url,
                }
                for item in (items.value or [])
            ]
        }

    except Exception as e:
        logging.error(f"Failed to list items: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/drives/{drive_id}/items/{item_id}")
async def get_item(user_email: str, drive_id: str, item_id: str):
    """
    Get metadata for a specific file or folder.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)
        item_id: The item ID
    """
    try:
        graph = await get_graph_client(user_email)

        if drive_id == "me":
            item = await graph.me.drive.items.by_drive_item_id(item_id).get()
        else:
            item = (
                await graph.drives.by_drive_id(drive_id)
                .items.by_drive_item_id(item_id)
                .get()
            )

        return {
            "id": item.id,
            "name": item.name,
            "type": "folder" if item.folder else "file",
            "size": item.size,
            "mime_type": item.file.mime_type if item.file else None,
            "created": (
                item.created_date_time.isoformat() if item.created_date_time else None
            ),
            "modified": (
                item.last_modified_date_time.isoformat()
                if item.last_modified_date_time
                else None
            ),
            "web_url": item.web_url,
            "parent_path": (
                item.parent_reference.path if item.parent_reference else None
            ),
        }

    except Exception as e:
        logging.error(f"Failed to get item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/drives/{drive_id}/items/{item_id}/content")
async def download_file(user_email: str, drive_id: str, item_id: str):
    """
    Download a file's content.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)
        item_id: The file item ID
    """
    try:
        graph = await get_graph_client(user_email)

        if drive_id == "me":
            drive_ref = graph.me.drive
        else:
            drive_ref = graph.drives.by_drive_id(drive_id)

        # Get file metadata for filename and content type
        item = await drive_ref.items.by_drive_item_id(item_id).get()
        if item.folder:
            raise HTTPException(status_code=400, detail="Cannot download a folder")

        # Get content
        content = await drive_ref.items.by_drive_item_id(item_id).content.get()

        return Response(
            content=content,
            media_type=item.file.mime_type if item.file else "application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={item.name}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to download file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/drives/{drive_id}/upload")
async def upload_file(request: Request, user_email: str, drive_id: str):
    """
    Upload a file to a drive.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)

    Query params:
        user_email: The user's email (for token lookup)

    Request body:
    {
        "path": "folder/filename.txt",  # Path including filename
        "content": "base64 encoded content",
        "content_type": "text/plain"  # Optional, for binary files
    }

    Note: For files > 4MB, use chunked upload (not implemented here).
    """
    try:
        data = await request.json()
        path = data.get("path")
        content = data.get("content")

        if not path or content is None:
            raise HTTPException(
                status_code=400, detail="Missing required fields: path, content"
            )

        graph = await get_graph_client(user_email)

        if drive_id == "me":
            drive_ref = graph.me.drive
        else:
            drive_ref = graph.drives.by_drive_id(drive_id)

        # Decode content if base64 encoded
        import base64

        try:
            file_bytes = base64.b64decode(content)
        except Exception:
            # Assume it's plain text
            file_bytes = content.encode("utf-8")

        # Upload (creates parent folders automatically)
        result = await drive_ref.root.item_with_path(path).content.put(file_bytes)

        return {
            "success": True,
            "id": result.id,
            "name": result.name,
            "web_url": result.web_url,
            "size": result.size,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to upload file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drives/{drive_id}/folder")
async def create_folder(request: Request, user_email: str, drive_id: str):
    """
    Create a folder.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)

    Request body:
    {
        "name": "New Folder",
        "parent_path": "Documents"  # Optional, defaults to root
    }
    """
    try:
        data = await request.json()
        folder_name = data.get("name")
        parent_path = data.get("parent_path")

        if not folder_name:
            raise HTTPException(status_code=400, detail="Missing required field: name")

        graph = await get_graph_client(user_email)

        if drive_id == "me":
            drive_ref = graph.me.drive
        else:
            drive_ref = graph.drives.by_drive_id(drive_id)

        new_folder = DriveItem(name=folder_name, folder=Folder())

        if parent_path:
            result = await drive_ref.root.item_with_path(parent_path).children.post(
                new_folder
            )
        else:
            result = await drive_ref.root.children.post(new_folder)

        return {
            "success": True,
            "id": result.id,
            "name": result.name,
            "web_url": result.web_url,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/drives/{drive_id}/items/{item_id}")
async def delete_item(user_email: str, drive_id: str, item_id: str):
    """
    Delete a file or folder.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)
        item_id: The item ID to delete
    """
    try:
        graph = await get_graph_client(user_email)

        if drive_id == "me":
            await graph.me.drive.items.by_drive_item_id(item_id).delete()
        else:
            await (
                graph.drives.by_drive_id(drive_id)
                .items.by_drive_item_id(item_id)
                .delete()
            )

        return {"success": True}

    except Exception as e:
        logging.error(f"Failed to delete item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Search
# =============================================================================


@router.get("/drives/{drive_id}/search")
async def search_files(user_email: str, drive_id: str, q: str):
    """
    Search for files in a drive.

    Path params:
        drive_id: The drive ID (use "me" for personal OneDrive)

    Query params:
        user_email: The user's email (for token lookup)
        q: Search query
    """
    try:
        graph = await get_graph_client(user_email)

        if drive_id == "me":
            results = await graph.me.drive.root.search_with_q(q).get()
        else:
            results = (
                await graph.drives.by_drive_id(drive_id).root.search_with_q(q).get()
            )

        return {
            "results": [
                {
                    "id": item.id,
                    "name": item.name,
                    "type": "folder" if item.folder else "file",
                    "path": (
                        item.parent_reference.path if item.parent_reference else None
                    ),
                    "web_url": item.web_url,
                }
                for item in (results.value or [])
            ]
        }

    except Exception as e:
        logging.error(f"Failed to search files: {e}")
        raise HTTPException(status_code=500, detail=str(e))
