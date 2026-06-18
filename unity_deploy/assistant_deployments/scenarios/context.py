"""Assistant-scoped execution context for scenario manager writes."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Awaitable

from droid.common.pipeline.types import IngestBinding

from unity_deploy.infra.workers.assistant_key_resolver import resolve_api_key
from unity_deploy.infra.workers.worker_utils import activate_unify_context


@dataclass(frozen=True)
class ScenarioIdentity:
    """Explicit assistant identity required before scenario state mutation."""

    user_id: str
    assistant_id: str
    project_name: str = "Assistants"


async def resolve_scenario_api_key(
    identity: ScenarioIdentity,
    *,
    resolver: Callable[[IngestBinding], Awaitable[str]] = resolve_api_key,
) -> str:
    """Resolve the Unify api_key for one scenario assistant identity."""

    return await resolver(
        IngestBinding(
            user_id=identity.user_id,
            assistant_id=identity.assistant_id,
        ),
    )


@asynccontextmanager
async def activate_scenario_context(
    identity: ScenarioIdentity,
    *,
    managers: list[type[Any]] | None = None,
    api_key: str | None = None,
    resolver: Callable[[IngestBinding], Awaitable[str]] = resolve_api_key,
    activator: Callable[..., None] = activate_unify_context,
) -> AsyncIterator[None]:
    """Activate `<user_id>/<assistant_id>` before mutating Droid managers."""

    resolved_api_key = api_key or await resolve_scenario_api_key(
        identity,
        resolver=resolver,
    )
    previous_key = os.environ.get("UNIFY_KEY")
    os.environ["UNIFY_KEY"] = resolved_api_key
    try:
        activator(
            identity.project_name,
            user_id=identity.user_id,
            assistant_id=identity.assistant_id,
            managers=managers,
        )
        yield
    finally:
        if previous_key is None:
            os.environ.pop("UNIFY_KEY", None)
        else:
            os.environ["UNIFY_KEY"] = previous_key
