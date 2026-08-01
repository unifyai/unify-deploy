"""A published catalogue snapshot is SQL run against the local database.

Snapshots are data-only COPY blocks, so anything else in one is either
corruption or an attempt to run statements as the database superuser. These
cover the structural gate and the conditions under which a fetch is attempted
at all; the download itself is not exercised here.
"""

from __future__ import annotations

import gzip
from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "selfhost" / "builtins_catalog_cache.sh"

VALID = b"COPY public.project FROM stdin;\n1\tBuiltins\n\\.\n"


def _run(snippet: str, env: dict[str, str] | None = None) -> int:
    return subprocess.run(
        ["bash", "-c", f"source {MODULE}\n{snippet}"],
        capture_output=True,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", **(env or {})},
    ).returncode


def _snapshot(tmp_path: Path, body: bytes, name: str = "s.sql.gz") -> Path:
    path = tmp_path / name
    path.write_bytes(gzip.compress(body))
    return path


def test_data_only_snapshot_is_accepted(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, VALID)
    assert _run(f"builtins_catalog_validate {snapshot}") == 0


def test_statement_between_blocks_is_rejected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, VALID + b"DROP TABLE project;\n")
    assert _run(f"builtins_catalog_validate {snapshot}") != 0


def test_copy_from_program_is_rejected(tmp_path: Path) -> None:
    """COPY ... FROM PROGRAM would run shell commands on the database host."""
    snapshot = _snapshot(
        tmp_path,
        VALID + b"COPY public.x FROM PROGRAM 'sh -c \"id\"';\n",
    )
    assert _run(f"builtins_catalog_validate {snapshot}") != 0


def test_unterminated_block_is_rejected(tmp_path: Path) -> None:
    """A truncated download must not be loaded as a partial catalogue."""
    snapshot = _snapshot(tmp_path, b"COPY public.project FROM stdin;\n1\tBuiltins\n")
    assert _run(f"builtins_catalog_validate {snapshot}") != 0


def test_empty_snapshot_is_rejected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, b"")
    assert _run(f"builtins_catalog_validate {snapshot}") != 0


def test_fetch_is_off_unless_a_url_is_configured(tmp_path: Path) -> None:
    assert _run(f"builtins_catalog_fetch key {tmp_path}/out.sql.gz") != 0
    assert not (tmp_path / "out.sql.gz").exists()


def test_fetch_requires_https(tmp_path: Path) -> None:
    """Snapshots execute as SQL, so they are never taken over plaintext."""
    code = _run(
        f"BUILTINS_CATALOG_URL=http://example.invalid "
        f"builtins_catalog_fetch key {tmp_path}/out.sql.gz",
    )
    assert code != 0
    assert not (tmp_path / "out.sql.gz").exists()


def test_failed_download_leaves_no_partial_file(tmp_path: Path) -> None:
    code = _run(
        f"BUILTINS_CATALOG_URL=https://unresolvable.invalid "
        f"BUILTINS_CATALOG_FETCH_TIMEOUT=5 "
        f"builtins_catalog_fetch key {tmp_path}/out.sql.gz",
    )
    assert code != 0
    assert not (tmp_path / "out.sql.gz").exists()
    assert not list(tmp_path.glob("*.download.*"))
