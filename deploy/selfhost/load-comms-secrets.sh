#!/usr/bin/env bash
set -euo pipefail

load_twilio_secret() {
  local secret_file="${SELF_HOST_COMMS_TWILIO_FILE:-/run/secrets/comms_twilio}"
  [[ -f "$secret_file" ]] || return 0

  local key value
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    key="${key#"${key%%[![:space:]]*}"}"
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      TWILIO_ACCOUNT_SID|TWILIO_AUTH_TOKEN|TWILIO_WA_ACCOUNT_SID|TWILIO_WA_AUTH_TOKEN)
        value="${value%$'\r'}"
        value="${value#\"}"
        value="${value%\"}"
        export "$key=$value"
        ;;
      *)
        echo "Unsupported key in Twilio secret file: $key" >&2
        return 1
        ;;
    esac
  done <"$secret_file"
}

load_gmail_secret() {
  local secret_file="${SELF_HOST_COMMS_GMAIL_FILE:-/run/secrets/comms_gmail}"
  [[ -s "$secret_file" ]] || return 0
  export GMAIL_BRIDGE_SA_FILE="$secret_file"
  GCP_SA_KEY="$(<"$secret_file")"
  export GCP_SA_KEY
}

load_twilio_secret
load_gmail_secret
