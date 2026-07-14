"""Provider-event dispatch inbox for Communication offline execution.

Temporary SQLite-backed launch-claim store for the initial offline provider-event
slice. It is container-local and not shared across Communication instances.
Replace it with Orchestra-backed downstream adoption once dispatch convergence
is wired, then remove this module, its settings, and the status-by-operation_id
route that reads from local storage.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DispatchInboxState = Literal["adopted", "launching", "launched", "terminal"]


@dataclass(frozen=True)
class DispatchInboxSnapshot:
    """Authorization fields captured when one dispatch operation is adopted."""

    run_key: str
    receipt_id: str
    accepted_activation_revision: str


@dataclass(frozen=True)
class DispatchInboxRecord:
    """One durable adoption record keyed by dispatch operation id."""

    operation_id: str
    run_id: int
    run_key: str
    receipt_id: str
    accepted_activation_revision: str
    state: DispatchInboxState
    launch_count: int
    job_name: str | None
    terminal_reason: str | None
    owns_launch: bool = False


class ProviderEventInboxMismatchError(ValueError):
    """Raised when a retry presents different authorization for one operation."""


class ProviderEventDispatchInbox:
    """Container-local SQLite inbox for owner-only offline job launch.

    Interim implementation only. Delete once Orchestra owns cross-instance
    adoption state for provider-event dispatch operations.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_event_dispatch_inbox (
                        operation_id TEXT PRIMARY KEY,
                        run_id INTEGER NOT NULL,
                        run_key TEXT NOT NULL DEFAULT '',
                        receipt_id TEXT NOT NULL DEFAULT '',
                        accepted_activation_revision TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        launch_count INTEGER NOT NULL DEFAULT 0,
                        job_name TEXT,
                        terminal_reason TEXT
                    )
                    """,
                )
                existing_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(provider_event_dispatch_inbox)",
                    )
                }
                for column_name, ddl in (
                    (
                        "run_key",
                        "ALTER TABLE provider_event_dispatch_inbox ADD COLUMN run_key TEXT NOT NULL DEFAULT ''",
                    ),
                    (
                        "receipt_id",
                        "ALTER TABLE provider_event_dispatch_inbox ADD COLUMN receipt_id TEXT NOT NULL DEFAULT ''",
                    ),
                    (
                        "accepted_activation_revision",
                        "ALTER TABLE provider_event_dispatch_inbox ADD COLUMN accepted_activation_revision TEXT NOT NULL DEFAULT ''",
                    ),
                    (
                        "job_name",
                        "ALTER TABLE provider_event_dispatch_inbox ADD COLUMN job_name TEXT",
                    ),
                    (
                        "terminal_reason",
                        "ALTER TABLE provider_event_dispatch_inbox ADD COLUMN terminal_reason TEXT",
                    ),
                ):
                    if column_name not in existing_columns:
                        connection.execute(ddl)
                connection.commit()

    def _record_from_row(self, row: sqlite3.Row) -> DispatchInboxRecord:
        return DispatchInboxRecord(
            operation_id=row["operation_id"],
            run_id=row["run_id"],
            run_key=row["run_key"],
            receipt_id=row["receipt_id"],
            accepted_activation_revision=row["accepted_activation_revision"],
            state=row["state"],
            launch_count=row["launch_count"],
            job_name=row["job_name"],
            terminal_reason=row["terminal_reason"],
        )

    def _assert_snapshot_matches(
        self,
        *,
        row: sqlite3.Row,
        run_id: int,
        snapshot: DispatchInboxSnapshot,
    ) -> None:
        if (
            int(row["run_id"]) != run_id
            or row["run_key"] != snapshot.run_key
            or row["receipt_id"] != snapshot.receipt_id
            or row["accepted_activation_revision"]
            != snapshot.accepted_activation_revision
        ):
            raise ProviderEventInboxMismatchError(
                "provider_event_dispatch_inbox_authorization_mismatch",
            )

    def adopt_or_get(
        self,
        *,
        operation_id: str,
        run_id: int,
        snapshot: DispatchInboxSnapshot,
    ) -> DispatchInboxRecord:
        """Insert or return the durable inbox row for one dispatch operation."""

        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO provider_event_dispatch_inbox (
                        operation_id,
                        run_id,
                        run_key,
                        receipt_id,
                        accepted_activation_revision,
                        state,
                        launch_count
                    ) VALUES (?, ?, ?, ?, ?, 'adopted', 0)
                    ON CONFLICT(operation_id) DO NOTHING
                    """,
                    (
                        operation_id,
                        run_id,
                        snapshot.run_key,
                        snapshot.receipt_id,
                        snapshot.accepted_activation_revision,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                connection.commit()
        assert row is not None
        self._assert_snapshot_matches(row=row, run_id=run_id, snapshot=snapshot)
        return self._record_from_row(row)

    def claim_launch(self, *, operation_id: str) -> DispatchInboxRecord:
        """Atomically claim launch ownership for one adopted operation."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                if row["state"] in {"launched", "terminal"}:
                    return self._record_from_row(row)
                updated = connection.execute(
                    """
                    UPDATE provider_event_dispatch_inbox
                    SET state = 'launching'
                    WHERE operation_id = ? AND state = 'adopted'
                    """,
                    (operation_id,),
                )
                if updated.rowcount == 0:
                    row = connection.execute(
                        """
                        SELECT operation_id, run_id, run_key, receipt_id,
                               accepted_activation_revision, state, launch_count,
                               job_name, terminal_reason
                        FROM provider_event_dispatch_inbox
                        WHERE operation_id = ?
                        """,
                        (operation_id,),
                    ).fetchone()
                    assert row is not None
                    return DispatchInboxRecord(
                        **{
                            **self._record_from_row(row).__dict__,
                            "owns_launch": False,
                        },
                    )
                connection.commit()
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                assert row is not None
                return DispatchInboxRecord(
                    **{
                        **self._record_from_row(row).__dict__,
                        "owns_launch": True,
                    },
                )

    def launch_if_owner(
        self,
        *,
        operation_id: str,
        job_name: str | None = None,
    ) -> DispatchInboxRecord:
        """Launch exactly once for the inbox owner of an adopted operation."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                if row["state"] == "launched":
                    return self._record_from_row(row)
                if row["state"] != "launching":
                    raise RuntimeError(
                        f"operation {operation_id} is not owned for launch",
                    )
                launch_count = int(row["launch_count"]) + 1
                connection.execute(
                    """
                    UPDATE provider_event_dispatch_inbox
                    SET state = 'launched',
                        launch_count = ?,
                        job_name = COALESCE(?, job_name)
                    WHERE operation_id = ?
                    """,
                    (launch_count, job_name, operation_id),
                )
                connection.commit()
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                assert row is not None
                return self._record_from_row(row)

    def get(self, *, operation_id: str) -> DispatchInboxRecord | None:
        """Return the durable inbox row for one dispatch operation."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    def mark_terminal(self, *, operation_id: str, reason: str) -> DispatchInboxRecord:
        """Record a terminal inbox outcome for one dispatch operation."""

        with self._lock:
            with self._connect() as connection:
                updated = connection.execute(
                    """
                    UPDATE provider_event_dispatch_inbox
                    SET state = 'terminal', terminal_reason = ?
                    WHERE operation_id = ?
                    """,
                    (reason, operation_id),
                )
                if updated.rowcount == 0:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                connection.commit()
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, run_key, receipt_id,
                           accepted_activation_revision, state, launch_count,
                           job_name, terminal_reason
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                assert row is not None
                return self._record_from_row(row)
