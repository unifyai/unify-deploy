#!/usr/bin/env python3
"""Verify the AssistantJobs system project is reachable with the admin key.

Fleet audit auth is ``ORCHESTRA_ADMIN_KEY`` → ``__system__`` on the
``AssistantJobs`` ``is_system`` project. There is no shared user API key.

Prerequisites:
  - ORCHESTRA_ADMIN_KEY must be set (in .env or env).

Usage:
  .venv/bin/python deploy/scripts/dev/verify_assistant_jobs_system.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import requests

PROD_URL = "https://api.unify.ai/v0"
STAGING_URL = "https://internal.example.com/v0"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _check_env(label: str, base_url: str, admin_key: str) -> bool:
    print(f"[{label}] Checking AssistantJobs as __system__...")
    resp = requests.get(
        f"{base_url}/logs",
        headers=_bearer(admin_key),
        params={
            "project_name": "AssistantJobs",
            "context": "startup_events",
            "limit": 1,
        },
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"       OK ({resp.status_code})")
        return True
    if resp.status_code == 404:
        # Project/context may be empty or context missing; project resolve worked
        # if detail is about context rather than project auth.
        detail = str(resp.json().get("detail", "")).lower()
        if "project" in detail and "not found" in detail:
            print(f"       FAIL: AssistantJobs project not found ({detail})")
            return False
        print(f"       OK-ish ({resp.status_code}): {resp.text[:200]}")
        return True
    print(f"       FAIL ({resp.status_code}): {resp.text[:300]}")
    return False


def main() -> int:
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if not admin_key:
        print("ERROR: ORCHESTRA_ADMIN_KEY is not set.", file=sys.stderr)
        return 1

    ok_prod = _check_env("prod", PROD_URL, admin_key)
    ok_staging = _check_env("staging", STAGING_URL, admin_key)
    if ok_prod and ok_staging:
        print("Done! AssistantJobs is reachable with ORCHESTRA_ADMIN_KEY.")
        return 0
    print(
        "ERROR: AssistantJobs system-project check failed. "
        "Confirm the Orchestra migration promoting AssistantJobs to is_system "
        "has been applied.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
