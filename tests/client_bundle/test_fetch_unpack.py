"""Tests for client-bundle archive unpacking."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from unify_deploy.client_bundle.fetch import unpack_bundle


def _write_archive(archive_path: Path, members: list[tarfile.TarInfo | tuple]) -> None:
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for member in members:
            if isinstance(member, tuple):
                name, payload = member
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                archive.addfile(member)


def test_unpack_bundle_extracts_regular_members(tmp_path: Path):
    archive_path = tmp_path / "bundle.tar.gz"
    _write_archive(
        archive_path,
        [("client/app.py", b"print('hi')\n"), ("client/data/rows.jsonl", b"{}\n")],
    )
    destination = tmp_path / "root"

    unpack_bundle(archive_path, destination)

    assert (destination / "client/app.py").read_bytes() == b"print('hi')\n"
    assert (destination / "client/data/rows.jsonl").read_bytes() == b"{}\n"


def test_unpack_bundle_refuses_traversal_member(tmp_path: Path):
    """A member escaping the destination must abort the unpack.

    The upstream sha256 check proves the archive is the one comms named; it
    says nothing about where the members want to be written.
    """
    archive_path = tmp_path / "bundle.tar.gz"
    _write_archive(archive_path, [("../escaped.txt", b"owned\n")])
    destination = tmp_path / "root"

    with pytest.raises(tarfile.FilterError):
        unpack_bundle(archive_path, destination)

    assert not (tmp_path / "escaped.txt").exists()


def test_unpack_bundle_refuses_symlink_out_of_tree(tmp_path: Path):
    """Symlinks pointing outside the bundle root must abort the unpack."""
    archive_path = tmp_path / "bundle.tar.gz"
    link = tarfile.TarInfo("client/passwd")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    _write_archive(archive_path, [link])
    destination = tmp_path / "root"

    with pytest.raises(tarfile.FilterError):
        unpack_bundle(archive_path, destination)

    assert not (destination / "client/passwd").exists()
