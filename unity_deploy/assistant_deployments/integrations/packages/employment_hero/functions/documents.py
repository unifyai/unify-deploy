"""Employment Hero documents (metadata only — file content not synced)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_documents(
    employee_id: str | None = None,
    document_type: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"documents": [
            {"id": "doc-1", "employee_id": "emp-mock-1",
             "name": "Right To Work — Passport.pdf",
             "type": "right_to_work", "signed_at": "2023-09-01"},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {"limit": min(limit, 100)}
    if employee_id:
        params["employee_id"] = employee_id
    if document_type:
        params["type"] = document_type
    body = await eh_get(org_path("/documents"), params=params)
    if "error" in body:
        return body
    return {"documents": body.get("data") or body.get("items") or []}


@custom_function()
async def list_employee_documents(employee_id: str, mock: bool = True) -> dict:
    if mock:
        return {"documents": [
            {"id": "doc-1", "employee_id": employee_id,
             "name": "Employment Contract — Signed.pdf",
             "type": "contract", "signed_at": "2023-09-01"},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/documents"))
    if "error" in body:
        return body
    return {"documents": body.get("data") or body.get("items") or []}


@custom_function()
async def get_document_metadata(document_id: str, mock: bool = True) -> dict:
    if mock:
        return {"id": str(document_id), "employee_id": "emp-mock-1",
                "name": "Employment Contract.pdf", "type": "contract",
                "signed_at": "2023-09-01", "size_bytes": 245_000}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/documents/{document_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_document_templates(mock: bool = True) -> dict:
    if mock:
        return {"templates": [
            {"id": "tpl-1", "name": "Employment Contract Template",
             "type": "contract", "country": "GB"},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/document_templates"))
    if "error" in body:
        return body
    return {"templates": body.get("data") or body.get("items") or []}


@custom_function()
async def sync_documents(mock: bool = False, since: str | None = None) -> dict:
    import datetime as _dt
    schema_version = "employment-hero.documents.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "documents": [{"id": "doc-1", "employee_id": "emp-mock-1",
                                "name": "Right To Work.pdf",
                                "type": "right_to_work",
                                "signed_at": "2023-09-01",
                                "size_bytes": 245_000,
                                "updated_at": started}],
                "document_templates": [{"id": "tpl-1",
                                         "name": "Employment Contract",
                                         "type": "contract", "country": "GB",
                                         "updated_at": started}],
            },
            "metadata": {"integration": "employment_hero",
                         "object_type": "documents", "started_at": started,
                         "mode": "mock"},
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate, org_path, _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    org_id, err = _org_id_or_error()
    if err is not None:
        err.update({"schema_version": schema_version, "tables": {}})
        return err
    cfg = get_employmenthero_config()

    doc_params: dict = {}
    if since:
        doc_params["updated_since"] = since
    docs_raw = await eh_paginate(
        org_path("/documents"),
        params=doc_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    documents = [{
        "id": d.get("id"),
        "employee_id": d.get("employee_id"),
        "name": d.get("name"),
        "type": d.get("type"),
        "signed_at": d.get("signed_at"),
        "uploaded_at": d.get("uploaded_at"),
        "size_bytes": d.get("size_bytes"),
        "mime_type": d.get("mime_type"),
        # NB: file URL is sensitive; included but expected to be a
        # short-lived signed URL.  Fetch on-demand via live API for
        # actual download flows.
        "url": d.get("url"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    } for d in docs_raw]

    tpl_raw = await eh_paginate(
        org_path("/document_templates"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    templates = [{
        "id": t.get("id"), "name": t.get("name"), "type": t.get("type"),
        "country": t.get("country"), "updated_at": t.get("updated_at"),
    } for t in tpl_raw]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"documents": documents, "document_templates": templates},
        "metadata": {"integration": "employment_hero", "object_type": "documents",
                     "organisation_id": org_id, "started_at": started,
                     "finished_at": finished, "since": since,
                     "row_counts": {"documents": len(documents),
                                    "document_templates": len(templates)}},
    }
