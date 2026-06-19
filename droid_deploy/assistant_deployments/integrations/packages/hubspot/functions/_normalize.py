"""HubSpot record envelope -> flat row dict.

HubSpot returns records shaped like ``{id, properties: {...},
createdAt, updatedAt, archived}``.  DataManager wants flat dicts with
column names matching schema.  This module flattens.

Underscore-prefixed so FunctionManager skips it.
"""

from __future__ import annotations


def normalize_object(record: dict, *, object_type: str) -> dict:
    """Generic flattener.  Returns ``{hubspot_id, ..props..,
    created_at, updated_at, archived, object_type}``.

    Property keys are kept as-is (HubSpot uses snake_case already).  Date
    fields with HubSpot ISO-8601 strings stay strings - DataManager
    handles type inference.
    """
    flat = {
        "hubspot_id": str(record.get("id", "")),
        "object_type": object_type,
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
        "archived": bool(record.get("archived", False)),
    }
    props = record.get("properties") or {}
    for key, value in props.items():
        flat[key] = value
    return flat


def normalize_contact(record: dict) -> dict:
    return normalize_object(record, object_type="contact")


def normalize_company(record: dict) -> dict:
    return normalize_object(record, object_type="company")


def normalize_deal(record: dict) -> dict:
    return normalize_object(record, object_type="deal")


def normalize_ticket(record: dict) -> dict:
    return normalize_object(record, object_type="ticket")


def normalize_line_item(record: dict) -> dict:
    return normalize_object(record, object_type="line_item")


def normalize_product(record: dict) -> dict:
    return normalize_object(record, object_type="product")


def normalize_quote(record: dict) -> dict:
    return normalize_object(record, object_type="quote")


def normalize_engagement(record: dict, *, engagement_type: str) -> dict:
    return normalize_object(record, object_type=f"engagement_{engagement_type}")


def normalize_owner(record: dict) -> dict:
    return {
        "owner_id": str(record.get("id", "")),
        "email": record.get("email", ""),
        "first_name": record.get("firstName", ""),
        "last_name": record.get("lastName", ""),
        "user_id": record.get("userId"),
        "team_id": (
            record.get("teams", [{}])[0].get("id") if record.get("teams") else None
        ),
        "archived": bool(record.get("archived", False)),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
    }


def normalize_pipeline_stage(pipeline: dict, stage: dict, *, object_type: str) -> dict:
    return {
        "pipeline_id": pipeline.get("id", ""),
        "stage_id": stage.get("id", ""),
        "object_type": object_type,
        "pipeline_label": pipeline.get("label", ""),
        "stage_label": stage.get("label", ""),
        "stage_display_order": stage.get("displayOrder"),
        "metadata": _stringify(stage.get("metadata") or {}),
        "archived": bool(stage.get("archived", False)),
    }


def normalize_property_def(record: dict, *, object_type: str) -> dict:
    return {
        "object_type": object_type,
        "name": record.get("name", ""),
        "label": record.get("label", ""),
        "description": record.get("description", ""),
        "type": record.get("type", ""),
        "field_type": record.get("fieldType", ""),
        "group_name": record.get("groupName", ""),
        "options": _stringify(record.get("options") or []),
        "calculated": bool(record.get("calculated", False)),
        "external_options": bool(record.get("externalOptions", False)),
        "hidden": bool(record.get("hidden", False)),
        "hubspot_defined": bool(record.get("hubspotDefined", False)),
    }


def normalize_list(record: dict) -> dict:
    return {
        "list_id": str(record.get("listId") or record.get("id", "")),
        "name": record.get("name", ""),
        "list_type": record.get("listType", ""),
        "processing_type": record.get("processingType", ""),
        "size": record.get("additionalProperties", {}).get("hs_list_size")
        or record.get("size"),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
        "deleted_at": record.get("deletedAt", ""),
    }


def normalize_form(record: dict) -> dict:
    return {
        "form_id": record.get("id", ""),
        "name": record.get("name", ""),
        "form_type": record.get("formType", ""),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
        "archived": bool(record.get("archived", False)),
    }


def normalize_workflow(record: dict) -> dict:
    return {
        "workflow_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "type": record.get("type", ""),
        "enabled": bool(record.get("enabled", False)),
        "created_at": record.get("insertedAt") or record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
    }


def normalize_marketing_email(record: dict) -> dict:
    return {
        "email_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "subject": record.get("subject", ""),
        "from_name": record.get("fromName", ""),
        "state": record.get("state", ""),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
        "publish_date": record.get("publishDate", ""),
    }


def normalize_sequence(record: dict) -> dict:
    return {
        "sequence_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "folder_id": record.get("folderId"),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
    }


def normalize_thread(record: dict) -> dict:
    return {
        "thread_id": str(record.get("id", "")),
        "inbox_id": record.get("inboxId"),
        "status": record.get("status", ""),
        "subject": record.get("subject", ""),
        "created_at": record.get("createdAt", ""),
        "latest_message_received": record.get("latestMessageReceivedAt", ""),
        "latest_message_sent": record.get("latestMessageSentAt", ""),
        "assigned_to": record.get("assignedTo"),
    }


def normalize_kb_article(record: dict) -> dict:
    return {
        "article_id": str(record.get("id", "")),
        "title": record.get("title", ""),
        "slug": record.get("slug", ""),
        "language": record.get("language", ""),
        "category_id": record.get("categoryId"),
        "subcategory_id": record.get("subcategoryId"),
        "current_state": record.get("currentState", ""),
        "url": record.get("url", ""),
        "html_title": record.get("htmlTitle", ""),
        "meta_description": record.get("metaDescription", ""),
        "created_at": record.get("created", "") or record.get("createdAt", ""),
        "updated_at": record.get("updated", "") or record.get("updatedAt", ""),
    }


def normalize_cms_page(record: dict) -> dict:
    return {
        "page_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "slug": record.get("slug", ""),
        "url": record.get("url", ""),
        "html_title": record.get("htmlTitle", ""),
        "meta_description": record.get("metaDescription", ""),
        "current_state": record.get("currentState", ""),
        "publish_date": record.get("publishDate", ""),
        "created_at": record.get("created", "") or record.get("createdAt", ""),
        "updated_at": record.get("updated", "") or record.get("updatedAt", ""),
    }


def normalize_blog_post(record: dict) -> dict:
    return {
        "post_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "slug": record.get("slug", ""),
        "url": record.get("url", ""),
        "html_title": record.get("htmlTitle", ""),
        "meta_description": record.get("metaDescription", ""),
        "blog_author_id": record.get("blogAuthorId"),
        "current_state": record.get("currentState", ""),
        "publish_date": record.get("publishDate", ""),
        "post_summary": record.get("postSummary", ""),
        "tag_ids": _stringify(record.get("tagIds") or []),
        "created_at": record.get("created", "") or record.get("createdAt", ""),
        "updated_at": record.get("updated", "") or record.get("updatedAt", ""),
    }


def normalize_file(record: dict) -> dict:
    return {
        "file_id": str(record.get("id", "")),
        "name": record.get("name", ""),
        "extension": record.get("extension", ""),
        "type": record.get("type", ""),
        "size": record.get("size"),
        "url": record.get("url", ""),
        "alt_text": record.get("alt", ""),
        "parent_folder_id": record.get("parentFolderId"),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
    }


def normalize_association(
    *,
    from_id: str,
    to_id: str,
    association_type: str,
    from_object_type: str,
    to_object_type: str,
) -> dict:
    return {
        "from_id": from_id,
        "to_id": to_id,
        "association_type": association_type,
        "from_object_type": from_object_type,
        "to_object_type": to_object_type,
    }


def normalize_custom_object_schema(record: dict) -> dict:
    return {
        "object_type": record.get("name", ""),
        "fully_qualified_name": record.get("fullyQualifiedName", ""),
        "labels_singular": (record.get("labels") or {}).get("singular", ""),
        "labels_plural": (record.get("labels") or {}).get("plural", ""),
        "primary_display_property": record.get("primaryDisplayProperty", ""),
        "secondary_display_properties": _stringify(
            record.get("secondaryDisplayProperties") or [],
        ),
        "required_properties": _stringify(record.get("requiredProperties") or []),
        "searchable_properties": _stringify(record.get("searchableProperties") or []),
        "associated_objects": _stringify(record.get("associatedObjects") or []),
        "object_type_id": record.get("objectTypeId", ""),
        "created_at": record.get("createdAt", ""),
        "updated_at": record.get("updatedAt", ""),
        "archived": bool(record.get("archived", False)),
    }


def _stringify(value) -> str:
    """Render lists/dicts as JSON strings.  DataManager prefers scalar columns."""
    import json

    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return str(value) if value is not None else ""
