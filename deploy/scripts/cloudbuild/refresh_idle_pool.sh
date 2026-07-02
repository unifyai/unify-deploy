#!/usr/bin/env bash
# Refresh the idle Unity job pool so pods pick up the just-deployed image and
# job manifest, then trim excess idle jobs. This is the single authoritative
# refresh for a deploy: it must run after the comms revision has taken traffic
# (the manifest that stamps pod env is produced by the live comms revision) and
# immediately after the GCS image hash is updated (new pods pull that image).
#
# Usage: refresh_idle_pool.sh <adapters_host>
# Requires env: ORCHESTRA_ADMIN_KEY.

set -euo pipefail

ADAPTERS_HOST="${1:?adapters host required}"
: "${ORCHESTRA_ADMIN_KEY:?ORCHESTRA_ADMIN_KEY required}"

ADAPTERS="https://${ADAPTERS_HOST}"
AUTH="Authorization: Bearer ${ORCHESTRA_ADMIN_KEY}"

echo "Removing stale-hash idle jobs before rotation..."
curl -sf -X POST -H "${AUTH}" "${ADAPTERS}/scheduled/jobs/cleanup" && echo

echo "Creating fresh idle jobs at the current image hash..."
curl -sf -X POST -H "${AUTH}" "${ADAPTERS}/scheduled/jobs/create?refresh=true" && echo

echo "Waiting 120s for jobs to register as idle..."
sleep 120

echo "Trimming excess idle jobs at the current hash..."
curl -sf -X POST -H "${AUTH}" "${ADAPTERS}/scheduled/jobs/cleanup" && echo
echo "Idle job refresh complete."
