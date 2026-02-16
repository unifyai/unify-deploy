#!/usr/bin/env python3
"""
One-time setup: Add GMP (Google Managed Prometheus) sidecar to Cloud Run services.

This script exports the current service config, injects a Prometheus collector
sidecar container, and applies the updated config. It preserves all existing
configuration (env vars, secrets, service account, startup probes, etc.).

After running this once per service, regular deploys via Cloud Build
(gcloud run deploy / gcloud run services update) will preserve the sidecar
automatically — only the main container image gets updated on each deploy.

Usage:
    # All services (staging + prod, adapters + comms)
    python scripts/setup_gmp_sidecar.py

    # Specific services only
    python scripts/setup_gmp_sidecar.py unity-adapters-staging unity-comms-app-staging

    # Dry run (prints the modified JSON without applying)
    python scripts/setup_gmp_sidecar.py --dry-run
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REGION = "us-central1"
PROJECT = "gcp-project-runtime"
SIDECAR_IMAGE = (
    "us-docker.pkg.dev/cloud-ops-agents-artifacts/"
    "cloud-run-gmp-sidecar/cloud-run-gmp-sidecar:1.2.1"
)

# service_name -> prometheus job_name
SERVICES = {
    "unity-adapters-staging": "adapters",
    "unity-adapters": "adapters",
    "unity-comms-app-staging": "comms",
    "unity-comms-app": "comms",
}

# Metadata fields that are read-only and must be stripped for replace
STRIP_METADATA_KEYS = {
    "creationTimestamp",
    "generation",
    "resourceVersion",
    "selfLink",
    "uid",
    "namespace",
}
STRIP_METADATA_ANNOTATIONS = {
    "run.googleapis.com/operation-id",
    "run.googleapis.com/urls",
    "run.googleapis.com/ingress-status",
    "serving.knative.dev/creator",
    "serving.knative.dev/lastModifier",
}
STRIP_TEMPLATE_ANNOTATIONS = {
    "run.googleapis.com/client-name",
    "run.googleapis.com/client-version",
}
STRIP_TEMPLATE_LABELS = {
    "client.knative.dev/nonce",
    "run.googleapis.com/startupProbeType",
}


def collection_config(job_name: str) -> str:
    return (
        "receivers:\n"
        "  prometheus:\n"
        "    config:\n"
        "      scrape_configs:\n"
        f"        - job_name: '{job_name}'\n"
        "          scrape_interval: 15s\n"
        "          metrics_path: /metrics\n"
        "          static_configs:\n"
        "            - targets: ['localhost:8080']\n"
        "exporters:\n"
        "  googlemanagedprometheus:\n"
        f"    project: {PROJECT}\n"
        "service:\n"
        "  pipelines:\n"
        "    metrics:\n"
        "      receivers: [prometheus]\n"
        "      exporters: [googlemanagedprometheus]\n"
    )


def export_service(service: str) -> dict:
    """Export the current Cloud Run service config as JSON."""
    result = subprocess.run(
        [
            "gcloud", "run", "services", "describe", service,
            "--region", REGION,
            "--project", PROJECT,
            "--format=json",
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def strip_readonly(spec: dict) -> dict:
    """Remove read-only fields that would cause replace to fail."""
    spec.pop("status", None)

    metadata = spec.get("metadata", {})
    for key in STRIP_METADATA_KEYS:
        metadata.pop(key, None)
    meta_ann = metadata.get("annotations", {})
    for key in STRIP_METADATA_ANNOTATIONS:
        meta_ann.pop(key, None)
    # Also strip client-name/version from top-level metadata
    for key in STRIP_TEMPLATE_ANNOTATIONS:
        meta_ann.pop(key, None)

    template_meta = spec.get("spec", {}).get("template", {}).get("metadata", {})
    tmpl_ann = template_meta.get("annotations", {})
    for key in STRIP_TEMPLATE_ANNOTATIONS:
        tmpl_ann.pop(key, None)
    tmpl_labels = template_meta.get("labels", {})
    for key in STRIP_TEMPLATE_LABELS:
        tmpl_labels.pop(key, None)

    return spec


def inject_sidecar(spec: dict, job_name: str) -> dict:
    """Add or update the GMP collector sidecar container."""
    template = spec["spec"]["template"]
    tmpl_ann = template.setdefault("metadata", {}).setdefault("annotations", {})
    containers = template["spec"]["containers"]

    # Find the main (ingress) container — the one with a ports section
    main_name = None
    for c in containers:
        if "ports" in c:
            main_name = c.get("name", "app")
            break
    if main_name is None:
        main_name = containers[0].get("name", "app")

    # Check if sidecar already exists
    sidecar_idx = None
    for i, c in enumerate(containers):
        if c.get("name") == "collector":
            sidecar_idx = i
            break

    sidecar_container = {
        "name": "collector",
        "image": SIDECAR_IMAGE,
        "env": [
            {
                "name": "COLLECTION_CONFIG",
                "value": collection_config(job_name),
            }
        ],
    }

    if sidecar_idx is not None:
        print(f"  Sidecar already exists — updating image and config")
        containers[sidecar_idx] = sidecar_container
    else:
        print(f"  Adding collector sidecar (depends on '{main_name}')")
        containers.append(sidecar_container)

    # Required annotations for multi-container
    tmpl_ann["run.googleapis.com/container-dependencies"] = (
        json.dumps({"collector": [main_name]})
    )

    # BETA launch stage required for multi-container
    meta_ann = spec.setdefault("metadata", {}).setdefault("annotations", {})
    meta_ann["run.googleapis.com/launch-stage"] = "BETA"

    return spec


def apply_service(spec: dict, dry_run: bool = False) -> None:
    """Write the modified spec to a temp file and apply via gcloud."""
    if dry_run:
        print(json.dumps(spec, indent=2))
        return

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(spec, f, indent=2)
        tmp_path = f.name

    try:
        subprocess.run(
            [
                "gcloud", "run", "services", "replace", tmp_path,
                "--region", REGION,
                "--project", PROJECT,
                "--quiet",
            ],
            check=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    targets = SERVICES
    if args:
        unknown = set(args) - set(SERVICES)
        if unknown:
            print(f"Unknown services: {', '.join(unknown)}")
            print(f"Available: {', '.join(SERVICES)}")
            sys.exit(1)
        targets = {k: v for k, v in SERVICES.items() if k in args}

    for service, job_name in targets.items():
        print(f"\n{'=' * 60}")
        print(f"  {service}")
        print(f"{'=' * 60}")

        print(f"  Exporting current config...")
        spec = export_service(service)

        print(f"  Stripping read-only fields...")
        spec = strip_readonly(spec)

        spec = inject_sidecar(spec, job_name)

        print(f"  {'DRY RUN — would apply:' if dry_run else 'Applying...'}")
        apply_service(spec, dry_run=dry_run)

        if not dry_run:
            print(f"  Done.")

    print(f"\n{'=' * 60}")
    if dry_run:
        print("Dry run complete. No changes applied.")
    else:
        print("All services updated.")
        print("Sidecars will be preserved by regular Cloud Build deploys.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
