"""Production-entrypoint regression tests for the self-host call controller."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_call_controller_retries_unchanged_tunnel_url_after_probe_failure(
    tmp_path: Path,
) -> None:
    """A failed early edge probe must not wedge an otherwise valid tunnel URL."""
    target_host = "retry-test.trycloudflare.com"
    minimum_probe_count = 4

    class BoundaryHandler(BaseHTTPRequestHandler):
        target_probe_count = 0
        count_lock = threading.Lock()

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def do_CONNECT(self) -> None:
            if self.path == f"{target_host}:443":
                with self.count_lock:
                    type(self).target_probe_count += 1
            self.send_response(502)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoundaryHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    tunnel_log = tmp_path / "call-tunnel.log"
    tunnel_log.write_text(
        f"quick tunnel ready at https://{target_host}\n",
        encoding="utf-8",
    )
    twilio_secret = tmp_path / "comms_twilio.env"
    twilio_secret.write_text(
        "\n".join(
            [
                "TWILIO_ACCOUNT_SID=test-main-sid",
                "TWILIO_AUTH_TOKEN=test-main-token",
                "TWILIO_WA_ACCOUNT_SID=test-whatsapp-sid",
                "TWILIO_WA_AUTH_TOKEN=test-whatsapp-token",
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    ready_file = tmp_path / "call-controller-ready"
    proxy_url = f"http://127.0.0.1:{server.server_port}"
    environment = {
        **os.environ,
        "HTTPS_PROXY": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "LIVEKIT_URL": "wss://test.livekit.invalid",
        "LIVEKIT_API_KEY": "test-livekit-key",
        "LIVEKIT_API_SECRET": "test-livekit-secret",
        "LIVEKIT_SIP_URI": "test.sip.invalid",
        "SELF_HOST_CALL_CONTROLLER_POLL_SECONDS": "0.1",
        "SELF_HOST_CALL_LEASE_CHECK_SECONDS": "1",
        "SELF_HOST_CALL_TUNNEL_LOG": str(tunnel_log),
        "SELF_HOST_CALL_CONTROLLER_READY_FILE": str(ready_file),
        "SELF_HOST_CALL_TUNNEL_URL_FILE": str(tmp_path / "call-tunnel-url"),
        "SELF_HOST_CM_HEALTH_URL": f"{proxy_url}/health",
        "SELF_HOST_COMMS_TWILIO_FILE": str(twilio_secret),
        "SELF_HOST_STATE_DIR": str(tmp_path),
    }
    process = subprocess.Popen(
        [
            "bash",
            str(
                REPO_ROOT / "deploy" / "selfhost" / "call-controller-entrypoint.sh",
            ),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with BoundaryHandler.count_lock:
                if BoundaryHandler.target_probe_count >= minimum_probe_count:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert BoundaryHandler.target_probe_count >= minimum_probe_count, output
    assert not ready_file.exists()
