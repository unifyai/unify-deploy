#!/usr/bin/env python3
"""Ensure a desktop file-sync SSH key exists on the Orchestra assistant row."""

from __future__ import annotations

import argparse
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _generate_keypair() -> tuple[str, str]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )
    return private_pem, f"{public_openssh} unity-file-sync"


def _fetch_existing(
    *,
    orchestra_url: str,
    admin_key: str,
    agent_id: int,
) -> tuple[str | None, str | None]:
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({"agent_id": str(agent_id)})
    url = f"{orchestra_url.rstrip('/')}/admin/assistant?{params}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {admin_key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assistants = payload.get("info") or []
    if not assistants:
        return None, None
    row = assistants[0]
    private_key = row.get("desktop_filesync_sshkey")
    if not private_key:
        return None, None
    return private_key, _public_from_private(private_key)


def _public_from_private(private_pem: str) -> str:
    private_key = serialization.load_ssh_private_key(
        private_pem.encode("utf-8"),
        password=None,
    )
    public_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )
    return f"{public_openssh} unity-file-sync"


def _store_private_key(
    *,
    orchestra_url: str,
    admin_key: str,
    agent_id: int,
    private_pem: str,
) -> None:
    import urllib.request

    url = f"{orchestra_url.rstrip('/')}/admin/assistant/{agent_id}"
    body = json.dumps({"desktop_filesync_sshkey": private_pem}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed to store SSH key: HTTP {resp.status}")


def ensure_self_host_desktop_ssh_key(
    *,
    orchestra_url: str,
    admin_key: str,
    agent_id: int,
) -> tuple[str, str]:
    """Return (private_pem, public_openssh) for the assistant."""
    existing_private, existing_public = _fetch_existing(
        orchestra_url=orchestra_url,
        admin_key=admin_key,
        agent_id=agent_id,
    )
    if existing_private and existing_public:
        return existing_private, existing_public

    private_pem, public_openssh = _generate_keypair()
    _store_private_key(
        orchestra_url=orchestra_url,
        admin_key=admin_key,
        agent_id=agent_id,
        private_pem=private_pem,
    )
    return private_pem, public_openssh


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument("--orchestra-url", default=os.environ.get("ORCHESTRA_URL", ""))
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("ORCHESTRA_ADMIN_KEY", ""),
    )
    parser.add_argument(
        "--output",
        choices=("public", "json"),
        default="public",
        help="Emit public key line or JSON with public_key",
    )
    args = parser.parse_args()

    if not args.orchestra_url or not args.admin_key:
        print("ORCHESTRA_URL and ORCHESTRA_ADMIN_KEY are required", file=sys.stderr)
        return 1

    _, public_key = ensure_self_host_desktop_ssh_key(
        orchestra_url=args.orchestra_url,
        admin_key=args.admin_key,
        agent_id=args.agent_id,
    )
    if args.output == "json":
        print(json.dumps({"public_key": public_key}))
    else:
        print(public_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
