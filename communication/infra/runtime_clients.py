"""Shared cached infra clients for Communication endpoints.

This module centralizes the small set of long-lived client factories that are
used across multiple infra domains. Callers import these helpers instead of
re-implementing credential loading or per-process caches inside large view
modules.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import HTTPException
from google.cloud import pubsub_v1
from google.oauth2.service_account import Credentials

from .helpers import setup_kubernetes_client

_pubsub_publisher: pubsub_v1.PublisherClient | None = None
_pubsub_subscriber: pubsub_v1.SubscriberClient | None = None
_cloud_tasks_client = None


def service_account_credentials() -> Credentials:
    """Build GCP service-account credentials from the configured env payload."""

    creds_json = os.getenv("GCP_SA_KEY")
    if not creds_json:
        raise RuntimeError("GCP_SA_KEY must be set for GCP-backed infra endpoints")
    return Credentials.from_service_account_info(json.loads(creds_json))


def get_pubsub_clients() -> (
    tuple[pubsub_v1.PublisherClient, pubsub_v1.SubscriberClient]
):
    """Return cached Pub/Sub publisher and subscriber clients."""

    global _pubsub_publisher, _pubsub_subscriber

    if _pubsub_publisher is None or _pubsub_subscriber is None:
        creds = service_account_credentials()
        _pubsub_publisher = pubsub_v1.PublisherClient(credentials=creds)
        _pubsub_subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    return _pubsub_publisher, _pubsub_subscriber


def get_cloud_tasks_client():
    """Return a cached Cloud Tasks client."""

    global _cloud_tasks_client

    if _cloud_tasks_client is None:
        from google.cloud import tasks_v2

        _cloud_tasks_client = tasks_v2.CloudTasksClient(
            credentials=service_account_credentials(),
        )
    return _cloud_tasks_client


async def get_k8s_clients():
    """Return cached Kubernetes API clients without blocking the event loop."""

    result = await asyncio.to_thread(setup_kubernetes_client)
    if not result[0]:
        raise HTTPException(
            status_code=500,
            detail="Failed to connect to Kubernetes cluster",
        )
    return result
