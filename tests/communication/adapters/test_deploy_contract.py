from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_adapters_dockerfile_serves_adapters_app() -> None:
    dockerfile = (ROOT / "Dockerfile-adapters").read_text(encoding="utf-8")

    assert "COPY adapters/ adapters/" in dockerfile
    assert 'RUN pip install --no-cache-dir ".[adapters]"' in dockerfile
    assert '"adapters.main:app"' in dockerfile
    assert '"communication.main:app"' not in dockerfile


def test_scheduled_cloudbuild_routes_target_adapters_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile-adapters").read_text(encoding="utf-8")
    assert '"adapters.main:app"' in dockerfile

    scheduled_routes = {
        "/scheduled/email-watches",
        "/scheduled/infra/maintenance",
        "/scheduled/microsoft-tokens",
        "/scheduled/google-tokens",
        "/scheduled/teams-watches",
        "/scheduled/cert-renewal",
    }
    for config_name in ("adapters.yaml", "adapters-staging.yaml"):
        config = (ROOT / "cloudbuild" / config_name).read_text(encoding="utf-8")
        for route in scheduled_routes:
            assert route in config, f"{route} missing from {config_name}"
