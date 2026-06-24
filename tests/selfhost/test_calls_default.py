import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_self_host_calls_enabled_by_default():
    script = REPO_ROOT / "selfhost" / "self_host_env.sh"
    completed = subprocess.run(
        [
            "bash",
            "-lc",
            f"source {script}; printf '%s' \"$SELF_HOST_CALLS_ENABLED\"",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "SELF_HOST_CALLS_ENABLED"},
    )

    assert completed.stdout == "1"


def test_sync_comms_defaults_to_voice_mode_without_public_url():
    script = REPO_ROOT / "selfhost" / "sync_comms_webhooks.py"
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "SELF_HOST_CALLS_ENABLED",
            "DROID_CONVERSATION_LOCAL_COMMS_PUBLIC_URL",
            "LOCAL_COMMS_PUBLIC_URL",
        }
    }

    completed = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 2
    assert (
        "--set-voice needs DROID_CONVERSATION_LOCAL_COMMS_PUBLIC_URL"
        in completed.stderr
    )
