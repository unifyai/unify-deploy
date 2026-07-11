"""Project native Unity-deploy integrations into the Builtins app catalog.

Unity-deploy native packages and provider-backed integrations have different
runtime owners:

* Native packages are enabled per assistant deployment and execute through
  FunctionManager functions, MCP configs, guidance, and secrets synced by this
  repository.
* Provider-backed apps are globally supported by Orchestra backends such as
  Composio or Pipedream, and become executable only after the user connects the
  app and Unity materializes provider tool rows.

The actor should not need to know those storage details when answering
"do we support Salesforce?" This module projects native package manifests into
the public-read Builtins app catalog only. It intentionally does not create
provider tool rows for native functions; executable native functions remain
discoverable through normal FunctionManager search.
"""

from __future__ import annotations

import json
from typing import Any

NATIVE_INTEGRATION_BACKEND_ID = "unity_native"
NATIVE_INTEGRATION_CACHE_VERSION = "unity-deploy-native-v1"


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def native_catalog_app_from_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one ``Integrations/Manifests`` row into a Builtins app record.

    The projection uses the same normalized app payload shape that provider
    backends use, with ``source_type='native'`` and native-only details in
    metadata. This gives Builtins enough text to semantically search native apps
    while leaving deployment activation and execution to Unity.
    """

    slug = str(row.get("slug") or "").strip()
    label = str(row.get("label") or slug.replace("_", " ").title()).strip()
    required_secrets = _json_list(row.get("required_secrets_json"))
    optional_secrets = _json_list(row.get("optional_secrets_json"))
    capabilities = _json_list(row.get("capability_ids_json"))
    function_names = _json_list(row.get("function_names_json"))
    guidance_titles = _json_list(row.get("guidance_titles_json"))
    tags = _json_list(row.get("tags_json"))
    return {
        "backend_id": NATIVE_INTEGRATION_BACKEND_ID,
        "provider_app_id": slug,
        "canonical_app_slug": slug,
        "display_name": label,
        "description": row.get("description") or label,
        "category": row.get("category") or row.get("tier") or "native",
        "auth_modes": ["native"],
        "available_scopes": [
            {
                "id": str(secret),
                "label": str(secret),
                "required": True,
            }
            for secret in required_secrets
        ],
        "available_actions": [
            {
                "id": str(capability),
                "name": str(capability),
                "activation_state": "deployment_scoped",
            }
            for capability in capabilities
        ],
        "source_type": "native",
        "tier": row.get("tier"),
        "quality": row.get("quality"),
        "capabilities": capabilities,
        "function_names": function_names,
        "guidance_titles": guidance_titles,
        "required_secrets": required_secrets,
        "optional_secrets": optional_secrets,
        "homepage": row.get("homepage") or "",
        "tags": tags,
        "raw_provider_metadata": {
            "source_type": "native",
            "native_metadata": {
                "tier": row.get("tier"),
                "quality": row.get("quality"),
                "capabilities": capabilities,
                "function_names": function_names,
                "guidance_titles": guidance_titles,
                "required_secrets": required_secrets,
                "optional_secrets": optional_secrets,
                "homepage": row.get("homepage") or "",
                "tags": tags,
            },
        },
    }


def native_catalog_apps_from_registry(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return app catalog payloads for valid native registry rows."""

    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        slug = str(row.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        apps.append(native_catalog_app_from_registry_row(row))
    return apps


def sync_integrations(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort publish of deployment integration records into Builtins.

    The existing DataManager registry sync remains the deployment telemetry
    source. This publish step feeds the global public-read app search index so
    native apps and provider-backed apps can appear in one `search_integrations`
    result set.
    """

    apps = native_catalog_apps_from_registry(rows)
    if not apps:
        return None

    from unify.integrations.builtins_catalog import seed_builtin_integrations

    changed = seed_builtin_integrations(
        apps=apps,
        backend_id=NATIVE_INTEGRATION_BACKEND_ID,
        app_slugs=[str(app["canonical_app_slug"]) for app in apps],
        prune_unlisted_apps=True,
    )
    return {
        "status": "synced" if changed else "unchanged",
        "backend_id": NATIVE_INTEGRATION_BACKEND_ID,
        "source_type": "native",
        "cache_version": NATIVE_INTEGRATION_CACHE_VERSION,
        "apps_upserted": len(apps) if changed else 0,
        "tools_upserted": 0,
    }
