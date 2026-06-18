#!/usr/bin/env bash
set -euo pipefail

exec python3 -m unity.gateway serve \
  --host "${UNITY_GATEWAY_HOST:-0.0.0.0}" \
  --port "${UNITY_GATEWAY_PORT:-8001}" \
  --mode all \
  --single-url
