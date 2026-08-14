from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_adapters_dockerfile_serves_adapters_app() -> None:
    dockerfile = (ROOT / "Dockerfile-adapters").read_text(encoding="utf-8")

    assert "COPY adapters/ adapters/" in dockerfile
    assert 'RUN pip install --no-cache-dir ".[adapters]"' in dockerfile
    assert '"adapters.main:app"' in dockerfile
    assert '"communication.main:app"' not in dockerfile


def test_scheduled_routes_are_registered_against_the_adapters_runtime() -> None:
    """Every Cloud Scheduler job must target the service serving adapters.

    The jobs were per-environment steps in cloudbuild/adapters{,-staging}.yaml
    until those configs were folded into the two orchestrators; the steps now
    live in one script both orchestrators run with the adapters service, which
    resolves that service's Cloud Run URL and appends each path. So the route
    list and the wiring that points it at adapters are what this pins.
    """
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
    script_path = Path("deploy/scripts/cloudbuild/configure_adapters_schedulers.sh")
    script = (ROOT / script_path).read_text(encoding="utf-8")
    for route in scheduled_routes:
        assert route in script, f"{route} missing from {script_path.name}"

    # A route registered in a script nothing runs schedules nothing, and each
    # orchestrator must hand it the adapters service rather than any other.
    for orchestrator in ("cloudbuild.yaml", "cloudbuild-staging.yaml"):
        config = (ROOT / "deploy" / orchestrator).read_text(encoding="utf-8")
        invocation = [line for line in config.splitlines() if script_path.name in line]
        assert invocation, f"{script_path.name} is never run by {orchestrator}"
        assert all(
            "${_ADAPTERS_SERVICE}" in line for line in invocation
        ), f"{orchestrator} does not target the adapters service"
