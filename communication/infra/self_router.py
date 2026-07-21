"""Router for pod-callable, self-scoped ``/infra/*`` endpoints.

Routes attached here are mounted at ``/infra`` WITHOUT the blanket admin-key
dependency. Each route authorizes with
:func:`communication.dependencies.authorize_admin_or_assistant`, which accepts
either the platform admin key (control-plane callers) or the assistant's own
``UNIFY_KEY`` verified against its ``AssistantSession`` (self-scoped). This lets
assistant pods make their own lifecycle/bundle calls without carrying the
shared ``ORCHESTRA_ADMIN_KEY``.

Kept as a standalone module so both ``views`` and ``task_execution`` can attach
routes to the same router without import cycles.
"""

from __future__ import annotations

from fastapi import APIRouter

assistant_self_router = APIRouter()
