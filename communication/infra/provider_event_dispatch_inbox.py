"""Durable provider-event dispatch inbox for Communication offline execution."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DispatchInboxState = Literal["adopted", "launching", "launched", "terminal"]


@dataclass(frozen=True)
class DispatchInboxRecord:
    """One durable adoption record keyed by dispatch operation id."""

    operation_id: str
    run_id: int
    state: DispatchInboxState
    launch_count: int


class ProviderEventDispatchInbox:
    """SQLite-backed inbox that owns execution launch for one operation id."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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
                        state TEXT NOT NULL,
                        launch_count INTEGER NOT NULL DEFAULT 0
                    )
                    """,
                )
                connection.commit()

    def adopt_or_get(self, *, operation_id: str, run_id: int) -> DispatchInboxRecord:
        """Insert or return the durable inbox row for one dispatch operation."""

        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO provider_event_dispatch_inbox (
                        operation_id, run_id, state, launch_count
                    ) VALUES (?, ?, 'adopted', 0)
                    ON CONFLICT(operation_id) DO NOTHING
                    """,
                    (operation_id, run_id),
                )
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, state, launch_count
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                connection.commit()
        assert row is not None
        return DispatchInboxRecord(
            operation_id=row["operation_id"],
            run_id=row["run_id"],
            state=row["state"],
            launch_count=row["launch_count"],
        )

    def claim_launch(self, *, operation_id: str) -> DispatchInboxRecord:
        """Atomically claim launch ownership for one adopted operation."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, state, launch_count
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                if row["state"] == "launched":
                    return DispatchInboxRecord(
                        operation_id=row["operation_id"],
                        run_id=row["run_id"],
                        state="launched",
                        launch_count=row["launch_count"],
                    )
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
                        SELECT operation_id, run_id, state, launch_count
                        FROM provider_event_dispatch_inbox
                        WHERE operation_id = ?
                        """,
                        (operation_id,),
                    ).fetchone()
                    assert row is not None
                    return DispatchInboxRecord(
                        operation_id=row["operation_id"],
                        run_id=row["run_id"],
                        state=row["state"],
                        launch_count=row["launch_count"],
                    )
                connection.commit()
                return DispatchInboxRecord(
                    operation_id=row["operation_id"],
                    run_id=row["run_id"],
                    state="launching",
                    launch_count=row["launch_count"],
                )

    def launch_if_owner(self, *, operation_id: str) -> DispatchInboxRecord:
        """Launch exactly once for the inbox owner of an adopted operation."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT operation_id, run_id, state, launch_count
                    FROM provider_event_dispatch_inbox
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                if row["state"] == "launched":
                    return DispatchInboxRecord(
                        operation_id=row["operation_id"],
                        run_id=row["run_id"],
                        state="launched",
                        launch_count=row["launch_count"],
                    )
                if row["state"] != "launching":
                    raise RuntimeError(
                        f"operation {operation_id} is not owned for launch",
                    )
                launch_count = int(row["launch_count"]) + 1
                connection.execute(
                    """
                    UPDATE provider_event_dispatch_inbox
                    SET state = 'launched', launch_count = ?
                    WHERE operation_id = ?
                    """,
                    (launch_count, operation_id),
                )
                connection.commit()
                return DispatchInboxRecord(
                    operation_id=row["operation_id"],
                    run_id=row["run_id"],
                    state="launched",
                    launch_count=launch_count,
                )
