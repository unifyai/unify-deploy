#!/usr/bin/env bash
set -euo pipefail

exec python3 -m droid.gateway serve \
  --host "${DROID_GATEWAY_HOST:-0.0.0.0}" \
  --port "${DROID_GATEWAY_PORT:-8001}" \
  --mode all \
  --single-url
