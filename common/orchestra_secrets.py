"""Upsert helper for assistant secrets stored in Orchestra.

Both ``store_microsoft_tokens`` and ``store_google_tokens`` need the same
"PUT, fall back to POST on 404" semantics so callers don't have to know
whether a given secret already exists.  Centralized here so the two
provider modules stay otherwise independent.
"""

from __future__ import annotations

import logging

import httpx

from common.settings import SETTINGS

logger = logging.getLogger(__name__)


async def _upsert_assistant_secrets(
    assistant_id: str,
    api_key: str,
    secrets: dict[str, str],
) -> bool:
    """Upsert each ``{name: value}`` against ``/assistant/{id}/secret``.

    Empty values are skipped (Orchestra rejects empty secret_value and
    they carry no information anyway).  Per-secret failures don't abort
    the batch — callers want every storable secret to land if at all
    possible — but the function returns ``False`` if anything failed.
    """
    base = f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret"
    headers = {"Authorization": f"Bearer {api_key}"}

    success = True
    async with httpx.AsyncClient() as client:
        for name, value in secrets.items():
            if not value:
                continue
            try:
                response = await client.put(
                    f"{base}/{name}",
                    json={"secret_value": value},
                    headers=headers,
                    timeout=30.0,
                )
                if response.status_code == 404:
                    response = await client.post(
                        base,
                        json={"secret_name": name, "secret_value": value},
                        headers=headers,
                        timeout=30.0,
                    )
                if response.status_code in (200, 201):
                    logger.info(f"Stored {name} for assistant {assistant_id}")
                else:
                    logger.info(
                        f"Failed to store {name}: {response.status_code} - {response.text}",
                    )
                    success = False
            except Exception as e:
                logger.info(f"Error storing {name}: {e}")
                success = False

    return success
