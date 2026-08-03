#!/usr/bin/env bash
# Run one ingestion worker: `parse` or `ingest`.
#
# One script for both because the two workers differ only in which handler they
# poll for. Keeping them in one file is what stops the pair from drifting apart
# in bootstrap -- environment, topic suffix, artifact root -- which would make
# a self-host reproduction of a hosted bug depend on which of two scripts had
# been kept current.
set -euo pipefail

role="${1:-}"
case "$role" in
  parse|ingest) ;;
  *)
    echo "usage: ingestion-worker-entrypoint.sh {parse|ingest}" >&2
    exit 2
    ;;
esac

# Ensures the topics and subscriptions exist before the worker polls. The
# emulator starts empty on every stack boot, so without this the first ingestion
# would fail on a missing subscription rather than simply waiting for work.
python3 - <<'PY'
import os

from google.cloud import pubsub_v1

project = os.environ.get("GCP_PROJECT_ID", "local-test-project")
suffix = os.environ.get("PUBSUB_TOPIC_SUFFIX", "-staging")

publisher = pubsub_v1.PublisherClient()
subscriber = pubsub_v1.SubscriberClient()

for base in ("unity-parse", "unity-ingest"):
    topic = publisher.topic_path(project, f"{base}{suffix}")
    try:
        publisher.create_topic(name=topic)
        print(f"created topic {topic}")
    except Exception as exc:  # already exists on a restart
        print(f"topic {topic}: {exc.__class__.__name__}")

    subscription = subscriber.subscription_path(project, f"{base}{suffix}-sub")
    try:
        subscriber.create_subscription(name=subscription, topic=topic)
        print(f"created subscription {subscription}")
    except Exception as exc:
        print(f"subscription {subscription}: {exc.__class__.__name__}")
PY

mkdir -p "${UNITY_SELFHOST_ARTIFACT_ROOT:-/artifacts}"

exec python3 -m "unify_deploy.infra.workers.entrypoint_${role}"
