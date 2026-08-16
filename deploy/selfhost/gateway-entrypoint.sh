#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
comms_enabled="${SELF_HOST_INTERNAL_COMMS_ENABLED:-false}"
calls_enabled="${SELF_HOST_INTERNAL_CALLS_ENABLED:-false}"
case "${calls_enabled,,}" in
  1|true|yes|on)
    # shellcheck source=load-comms-secrets.sh
    source "$SCRIPT_DIR/load-comms-secrets.sh"
    ;;
  *)
    case "${comms_enabled,,}" in
      1|true|yes|on)
        # shellcheck source=load-comms-secrets.sh
        source "$SCRIPT_DIR/load-comms-secrets.sh"
        unset TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN
        unset TWILIO_WA_ACCOUNT_SID TWILIO_WA_AUTH_TOKEN
        ;;
    esac
    ;;
esac

exec python3 -m unify.gateway serve \
  --host "${UNIFY_GATEWAY_HOST:-0.0.0.0}" \
  --port "${UNIFY_GATEWAY_PORT:-8001}" \
  --log-level "${UNIFY_GATEWAY_LOG_LEVEL:-debug}" \
  --mode all \
  --single-url
