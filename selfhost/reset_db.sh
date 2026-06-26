#!/usr/bin/env bash
# Reset the local self-host database to the manual owner state, or pre-signup when no owner exists.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEPLOY_REPO_PATH="$(cd "$SCRIPT_DIR/.." && pwd -P)"
UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO_PATH/.." && pwd -P)}"
UNITY_REPO_PATH="${UNITY_REPO_PATH:-$UNIFY_STACK_ROOT/unity}"
CONSOLE_REPO_PATH="${CONSOLE_REPO_PATH:-$UNIFY_STACK_ROOT/console}"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$UNIFY_STACK_ROOT/orchestra}"
SELF_HOST_ENV_SCRIPT="$SCRIPT_DIR/self_host_env.sh"

YES=false
STOP_RUNTIME=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      YES=true
      shift
      ;;
    --keep-runtime)
      STOP_RUNTIME=false
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage: $(basename "$0") [--yes] [--keep-runtime]

Deletes local self-host org/assistant/project history. If a manually-created
owner exists, preserves that owner, API key, and personal Coordinator. If no
owner exists yet, returns to pre-signup platform defaults with no users.

Options:
  --yes           Run without an interactive confirmation prompt.
  --keep-runtime  Leave the current Unity runtime process running.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$ORCHESTRA_REPO_PATH" ]]; then
  echo "Orchestra repo not found at $ORCHESTRA_REPO_PATH" >&2
  exit 1
fi

if [[ "$YES" != "true" ]]; then
  echo "This will delete local self-host chat, onboarding, org, and assistant history."
  read -r -p "Type reset to continue: " confirmation
  if [[ "$confirmation" != "reset" ]]; then
    echo "Reset cancelled."
    exit 1
  fi
fi

export SELF_HOST=1
export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
  # shellcheck disable=SC1090
  source "$SELF_HOST_ENV_SCRIPT"
  export_self_host_coordinator_runtime_file
  if [[ -f "$UNITY_REPO_PATH/.env" ]]; then
    load_self_host_env_file "$UNITY_REPO_PATH/.env"
  fi
fi

if [[ -z "${ORCHESTRA_DB_PORT:-}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    ORCHESTRA_DB_PORT="$(docker port orchestra-local-db 5432/tcp 2>/dev/null | sed -n 's/.*://p' | head -1 || true)"
  fi
  export ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-55432}"
fi
export ORCHESTRA_DB_HOST="${ORCHESTRA_DB_HOST:-127.0.0.1}"
export ORCHESTRA_DB_USER="${ORCHESTRA_DB_USER:-orchestra}"
export ORCHESTRA_DB_PASS="${ORCHESTRA_DB_PASS:-orchestra}"
export ORCHESTRA_DB_BASE="${ORCHESTRA_DB_BASE:-orchestra}"

if [[ "$STOP_RUNTIME" == "true" && -f "$CONSOLE_REPO_PATH/scripts/local.sh" ]]; then
  UNITY_ALLOW_RUNTIME_STOP=1 SELF_HOST=1 bash "$CONSOLE_REPO_PATH/scripts/local.sh" stop-runtime-backend >/dev/null 2>&1 || true
fi

cd "$ORCHESTRA_REPO_PATH"

python_bin="python3"
if [[ -x "$ORCHESTRA_REPO_PATH/.venv/bin/python" ]]; then
  python_bin="$ORCHESTRA_REPO_PATH/.venv/bin/python"
elif command -v poetry >/dev/null 2>&1; then
  python_bin="poetry run python"
fi

$python_bin <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

import orchestra.db.models.artifact_embedding_models  # noqa: F401
import orchestra.db.models.core_models  # noqa: F401
import orchestra.db.models.integration_provider_models  # noqa: F401
import orchestra.db.models.orchestra_models  # noqa: F401
from orchestra.db.base import Base
from orchestra.db.models.orchestra_models import (
    ApiKey,
    Assistant,
    BillingAccount,
    OnboardingStatus,
    Organization,
    User,
)
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.services.coordinator_service import (
    COORDINATOR_DEFAULT_DESKTOP_MODE,
    COORDINATOR_DEFAULT_FIRST_NAME,
    COORDINATOR_DEFAULT_JOB_TITLE,
    COORDINATOR_DEFAULT_NATIONALITY,
    create_personal_coordinator,
)
from orchestra.services.self_host_bootstrap import (
    ensure_platform_billing_defaults,
    ensure_provider_integration_backends,
)
from orchestra.settings import settings


if not settings.is_self_host:
    raise SystemExit("SELF_HOST=1 is required for local reset")

engine = create_engine(str(settings.db_url), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
identifier_preparer = engine.dialect.identifier_preparer
reset_api_key = os.environ.get("SELF_HOST_RESET_API_KEY", "").strip()


def quote_identifier(value: str) -> str:
    return identifier_preparer.quote(value)


def table_sql(table_name: str) -> str:
    return f"public.{quote_identifier(table_name)}"


def normalize_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def owner_display_name(owner: User) -> str | None:
    parts = [
        getattr(owner, "name", None),
        getattr(owner, "last_name", None),
        getattr(owner, "surname", None),
    ]
    display_name = " ".join(str(part).strip() for part in parts if str(part or "").strip())
    return display_name or None


def delete_matching(session, table_name: str, columns: set[str], values: dict[str, list[Any]]) -> int:
    clauses: list[str] = []
    bindparams = []
    params: dict[str, list[Any]] = {}

    for column, key in (
        ("assistant_id", "assistant_ids"),
        ("agent_id", "assistant_ids"),
        ("owner_user_id", "all_user_ids"),
        ("organization_id", "organization_ids"),
        ("org_id", "organization_ids"),
        ("team_id", "team_ids"),
        ("target_team_id", "team_ids"),
        ("user_id", "all_user_ids"),
    ):
        column_values = values.get(key) or []
        if column in columns and column_values:
            param_name = f"{column}_{key}"
            clauses.append(f"{quote_identifier(column)} IN :{param_name}")
            bindparams.append(bindparam(param_name, expanding=True))
            params[param_name] = column_values

    if not clauses:
        return 0

    statement = text(f"DELETE FROM {table_sql(table_name)} WHERE {' OR '.join(clauses)}")
    if bindparams:
        statement = statement.bindparams(*bindparams)
    result = session.execute(statement, params)
    return int(result.rowcount or 0)


def reset_owner_onboarding(session, owner_id: str) -> None:
    session.execute(
        text("DELETE FROM onboarding_status WHERE user_id = :owner_id"),
        {"owner_id": owner_id},
    )
    session.add(OnboardingStatus(user_id=owner_id, current_step="workspace_setup", step_data={}))
    session.flush()


def reset_coordinator_profile(coordinator: Assistant) -> None:
    coordinator.first_name = COORDINATOR_DEFAULT_FIRST_NAME
    coordinator.surname = None
    coordinator.job_title = COORDINATOR_DEFAULT_JOB_TITLE
    coordinator.age = None
    coordinator.nationality = COORDINATOR_DEFAULT_NATIONALITY
    coordinator.profile_photo = None
    coordinator.profile_video = None
    coordinator.desktop_mode = COORDINATOR_DEFAULT_DESKTOP_MODE
    coordinator.about = ""
    coordinator.weekly_limit = None
    coordinator.monthly_spending_cap = None
    coordinator.monthly_spending_cap_set_at = None
    coordinator.max_parallel = None
    coordinator.last_followup_sent_at = None
    coordinator.inactivity_followup_opted_out = False
    coordinator.voice_id = None
    coordinator.voice_provider = None
    coordinator.is_local = False


def runtime_api_key() -> str:
    state_dir = Path(
        os.environ.get("SELF_HOST_STATE_DIR")
        or os.environ.get("UNITY_HOME")
        or Path.home() / ".unity",
    )
    runtime_file = Path(
        os.environ.get("SELF_HOST_COORDINATOR_RUNTIME_FILE")
        or state_dir / "coordinator-runtime.json",
    )
    if not runtime_file.exists():
        return ""
    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return (data.get("apiKey") or data.get("api_key") or "").strip()


def resolve_owner_by_api_key(session, api_key: str) -> User | None:
    if not api_key:
        return None
    return session.scalar(
        select(User)
        .join(ApiKey, ApiKey.user_id == User.id)
        .where(ApiKey.key == api_key),
    )


def resolve_reset_owner(session) -> User | None:
    if reset_api_key:
        owner = resolve_owner_by_api_key(session, reset_api_key)
        if owner is None:
            raise RuntimeError("SELF_HOST_RESET_API_KEY does not match a local user")
        return owner
    return resolve_owner_by_api_key(session, runtime_api_key())


with SessionLocal() as session:
    ensure_platform_billing_defaults(session)
    ensure_provider_integration_backends(session)
    session.commit()

with SessionLocal() as session:
    owner = resolve_reset_owner(session)

    coordinator = None
    keep_assistant_id = None
    if owner is not None:
        coordinator = session.scalar(
            select(Assistant).where(
                Assistant.user_id == owner.id,
                Assistant.organization_id.is_(None),
                Assistant.is_coordinator.is_(True),
            ),
        )
        if coordinator is None:
            coordinator, _ = create_personal_coordinator(session, owner.id)
            session.flush()
        keep_assistant_id = int(coordinator.agent_id)

    assistant_ids = [
        int(value)
        for value in session.scalars(select(Assistant.agent_id)).all()
        if keep_assistant_id is None or int(value) != keep_assistant_id
    ]
    organization_ids = [int(value) for value in session.scalars(select(Organization.id)).all()]
    user_ids_to_delete = [
        str(value)
        for value in session.scalars(
            select(User.id) if owner is None else select(User.id).where(User.id != owner.id),
        ).all()
    ]
    all_user_ids = (
        user_ids_to_delete
        if owner is None
        else [str(owner.id), *user_ids_to_delete]
    )

    team_ids = [
        int(row[0])
        for row in session.execute(
            text("SELECT id FROM team WHERE organization_id IN :org_ids").bindparams(
                bindparam("org_ids", expanding=True),
            ),
            {"org_ids": organization_ids or [-1]},
        ).all()
    ]

    values = {
        "assistant_ids": assistant_ids,
        "organization_ids": organization_ids,
        "team_ids": team_ids,
        "all_user_ids": all_user_ids,
    }

    protected_tables = {
        "alembic_version",
        "api_key",
        "billing_account",
        "billing_plan_assignment",
        "billing_plan_template",
        "email_account",
        "integration_backends",
        "integration_bootstrap_state",
        "integration_overlays",
        "permission",
        "plan_group",
        "plan_group_member",
        "recharge_type",
        "role_permission",
        "role",
        "user",
    }
    special_tables = {"assistants", "organization", "project", "onboarding_status", "voices"}

    table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names(schema="public"))
    ordered_tables = [name for name in table_names if name in existing_tables]
    ordered_tables.extend(sorted(existing_tables - set(table_names)))

    deleted_rows = 0
    for table_name in ordered_tables:
        if table_name in protected_tables or table_name in special_tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name, schema="public")}
        deleted_rows += delete_matching(session, table_name, columns, values)

    deleted_projects = session.execute(
        text(
            """
            DELETE FROM project
            WHERE COALESCE(is_public_read, false) = false
              AND (user_id IS NOT NULL OR organization_id IS NOT NULL)
            """,
        ),
    ).rowcount or 0
    deleted_assistants = session.execute(
        text(
            "DELETE FROM assistants"
            if keep_assistant_id is None
            else "DELETE FROM assistants WHERE agent_id <> :keep_assistant_id",
        ),
        {} if keep_assistant_id is None else {"keep_assistant_id": keep_assistant_id},
    ).rowcount or 0
    if coordinator is not None:
        reset_coordinator_profile(coordinator)
    session.flush()
    deleted_api_keys = 0
    if user_ids_to_delete:
        deleted_api_keys = session.execute(
            text('DELETE FROM api_key WHERE user_id IN :user_ids').bindparams(
                bindparam("user_ids", expanding=True),
            ),
            {"user_ids": user_ids_to_delete},
        ).rowcount or 0
        if "email_account" in existing_tables:
            session.execute(
                text('DELETE FROM email_account WHERE user_id IN :user_ids').bindparams(
                    bindparam("user_ids", expanding=True),
                ),
                {"user_ids": user_ids_to_delete},
            )
        if "onboarding_status" in existing_tables:
            session.execute(
                text('DELETE FROM onboarding_status WHERE user_id IN :user_ids').bindparams(
                    bindparam("user_ids", expanding=True),
                ),
                {"user_ids": user_ids_to_delete},
            )
    deleted_voices = 0
    if "voices" in existing_tables:
        deleted_voices = session.execute(
            text('DELETE FROM voices WHERE user_id IN :voice_user_ids').bindparams(
                bindparam("voice_user_ids", expanding=True),
            ),
            {"voice_user_ids": all_user_ids or ["__none__"]},
        ).rowcount or 0
    deleted_organizations = session.execute(text("DELETE FROM organization")).rowcount or 0
    deleted_users = 0
    if user_ids_to_delete:
        deleted_users = session.execute(
            text('DELETE FROM "user" WHERE id IN :user_ids').bindparams(
                bindparam("user_ids", expanding=True),
            ),
            {"user_ids": user_ids_to_delete},
        ).rowcount or 0

    session.execute(
        text(
            """
            DELETE FROM billing_account ba
            WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.billing_account_id = ba.id)
              AND NOT EXISTS (SELECT 1 FROM organization o WHERE o.billing_account_id = ba.id)
            """,
        ),
    )
    session.execute(
        text(
            """
            DELETE FROM log_unique_constraint luc
            WHERE NOT EXISTS (SELECT 1 FROM context c WHERE c.id = luc.context_id)
            """,
        ),
    )

    default_tasks = None
    if owner is not None:
        reset_owner_onboarding(session, owner.id)
        coordinator, _ = create_personal_coordinator(session, owner.id)
        reset_coordinator_profile(coordinator)
        default_tasks = DefaultTasksSeeder.seed(session, user_id=str(owner.id))
    session.commit()

with SessionLocal() as session:
    owner = resolve_reset_owner(session)
    coordinator = None
    api_key = None
    billing_account = None
    if owner is not None:
        coordinator = session.scalar(
            select(Assistant).where(
                Assistant.user_id == owner.id,
                Assistant.organization_id.is_(None),
                Assistant.is_coordinator.is_(True),
            ),
        )
        api_key = (
            session.scalar(
                select(ApiKey.key).where(
                    ApiKey.user_id == owner.id,
                    ApiKey.organization_id.is_(None),
                ),
            )
            or reset_api_key
        )
        billing_account = session.get(BillingAccount, owner.billing_account_id)
    payload = {
        "ok": True,
        "pre_signup": owner is None,
        "user_id": owner.id if owner is not None else None,
        "email": owner.email if owner is not None else None,
        "api_key": api_key,
        "coordinator_agent_id": coordinator.agent_id if coordinator is not None else None,
        "deleted_assistants": int(deleted_assistants),
        "deleted_organizations": int(deleted_organizations),
        "deleted_users": int(deleted_users),
        "deleted_api_keys": int(deleted_api_keys),
        "deleted_voices": int(deleted_voices),
        "deleted_projects": int(deleted_projects),
        "deleted_scoped_rows": int(deleted_rows),
        "default_tasks": default_tasks,
        "credits": normalize_json(billing_account.credits if billing_account else None),
    }
    state_dir = Path(os.environ.get("SELF_HOST_STATE_DIR") or os.environ.get("UNITY_HOME") or Path.home() / ".unity")
    if owner is not None and coordinator is not None and api_key:
        write_json_file(
            state_dir / "coordinator-runtime.json",
            {
                "apiKey": api_key,
                "coordinatorAgentId": str(coordinator.agent_id),
            },
        )
        write_json_file(
            state_dir / "self-host-owner.json",
            {
                "userId": str(owner.id),
                "email": owner.email,
                "name": owner_display_name(owner),
            },
        )
    else:
        for filename in ("coordinator-runtime.json", "self-host-owner.json", "runtime-state.json"):
            (state_dir / filename).unlink(missing_ok=True)
    print(json.dumps(payload, sort_keys=True))
PY
