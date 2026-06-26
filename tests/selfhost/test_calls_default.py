import os
import subprocess
import sys
import tempfile
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
            "UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL",
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
        "--set-voice needs UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL"
        in completed.stderr
    )


def test_livekit_cloud_state_overrides_repo_dev_env():
    script = REPO_ROOT / "selfhost" / "self_host_env.sh"
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        repo_env = state_dir / "unity.env"
        repo_env.write_text(
            "\n".join(
                [
                    "LIVEKIT_URL=ws://localhost:7880",
                    "LIVEKIT_API_KEY=devkey",
                    "LIVEKIT_API_SECRET=secret",
                    "LIVEKIT_SIP_URI=dev.sip.invalid",
                ],
            )
            + "\n",
            encoding="utf-8",
        )
        (state_dir / "livekit_cloud.env").write_text(
            "\n".join(
                [
                    "LIVEKIT_URL=wss://cloud.livekit.example",
                    "LIVEKIT_API_KEY=cloud-key",
                    "LIVEKIT_API_SECRET=cloud-secret",
                    "LIVEKIT_SIP_URI=cloud.sip.livekit.example",
                ],
            )
            + "\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                "bash",
                "-lc",
                (
                    f"source {script}; "
                    f"load_self_host_repo_env_file {repo_env}; "
                    "self_host_export_livekit_backend; "
                    "printf '%s|%s|%s|%s' "
                    '"$LIVEKIT_URL" "$LIVEKIT_API_KEY" "$LIVEKIT_API_SECRET" "$LIVEKIT_SIP_URI"'
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "UNITY_HOME": str(state_dir),
                "SELF_HOST_STATE_DIR": str(state_dir),
            },
        )

    assert (
        completed.stdout
        == "wss://cloud.livekit.example|cloud-key|cloud-secret|cloud.sip.livekit.example"
    )


def test_calls_disabled_still_uses_cloud_livekit_media():
    script = REPO_ROOT / "selfhost" / "self_host_env.sh"
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        (state_dir / "livekit_cloud.env").write_text(
            "\n".join(
                [
                    "LIVEKIT_URL=wss://cloud.livekit.example",
                    "LIVEKIT_API_KEY=cloud-key",
                    "LIVEKIT_API_SECRET=cloud-secret",
                ],
            )
            + "\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                "bash",
                "-lc",
                (
                    f"source {script}; "
                    "self_host_export_livekit_backend; "
                    "self_host_calls_enabled && calls=enabled || calls=disabled; "
                    "printf '%s|%s|%s|%s' "
                    '"$calls" "$LIVEKIT_URL" "$LIVEKIT_API_KEY" "$LIVEKIT_API_SECRET"'
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "UNITY_HOME": str(state_dir),
                "SELF_HOST_STATE_DIR": str(state_dir),
                "SELF_HOST_CALLS_ENABLED": "0",
            },
        )

    assert (
        completed.stdout
        == "disabled|wss://cloud.livekit.example|cloud-key|cloud-secret"
    )


def test_localhost_livekit_url_is_not_source_stack_media_configured():
    script = REPO_ROOT / "selfhost" / "self_host_env.sh"
    completed = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f"source {script}; "
                "self_host_livekit_media_configured && printf configured || printf missing"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LIVEKIT_URL": "ws://localhost:7880",
            "LIVEKIT_API_KEY": "devkey",
            "LIVEKIT_API_SECRET": "secret",
        },
    )

    assert completed.stdout == "missing"
