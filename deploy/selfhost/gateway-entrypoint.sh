#!/usr/bin/env bash
set -euo pipefail

exec python3 -m unity.gateway serve \
  --host "${UNITY_GATEWAY_HOST:-0.0.0.0}" \
  --port "${UNITY_GATEWAY_PORT:-8001}" \
  --log-level "${UNITY_GATEWAY_LOG_LEVEL:-debug}" \
  --mode all \
  --single-url
