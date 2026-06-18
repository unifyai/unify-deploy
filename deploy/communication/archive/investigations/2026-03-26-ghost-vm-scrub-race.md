# Ghost VM Scrub Race Condition

**Date**: 2026-03-26
**Severity**: Medium — causes VM pool exhaustion under concurrent load
**Discovered during**: Stress test with 15 assistants on preview

## Symptom

10 of 15 assistants never received a VM after 180s. The VM pool showed
`INV-12: VM pool exhausted: 0 idle VMs` despite having 30+ VMs in the project.

## Root Cause

Race condition between `_scrub_inconsistent_vms()` and `_start_one_stopped_vm()`:

1. `replenish_pool` calls `_scrub_inconsistent_vms` first (line 1190 of `vm_helpers.py`)
2. `_scrub_inconsistent_vms` stops any VM with `pool-role=stopped` + GCE status `RUNNING`
3. `replenish_pool` then calls `_start_one_stopped_vm` which starts stopped VMs
4. `_start_one_stopped_vm` does NOT update the label — it relies on the VM's
   startup script to call `POST /infra/vm/mark-idle` after boot (~30-60s)
5. During that 30-60s boot window, the NEXT replenish call's scrub sees the
   VM as `pool-role=stopped` + `RUNNING` and stops it again
6. The VM never finishes booting, never calls mark-idle, never becomes idle

This creates an infinite start-stop cycle where VMs are started by replenish
and immediately killed by scrub before they can transition to idle.

## Evidence

```
gcloud compute instances list --project=droid-assistant-vms --zones=us-central1-b \
  --filter="labels.pool-role=stopped AND status=RUNNING"

→ 10 ghost VMs: all labeled stopped but RUNNING
→ These were started by replenish but scrubbed before boot completed
```

Cloud logs showed no `scrub` or `mark-idle` activity — confirming the VMs
never reached the point of calling the startup script's mark-idle endpoint.

## Fix Options

**Option A (simplest)**: Set `pool-role=idle` immediately in `_start_one_stopped_vm`
after the VM starts, before the startup script runs. The startup script's
`mark-idle` call becomes a no-op (label already set).

**Option B**: Track recently-started VMs (e.g., with a timestamp label) and
exclude VMs started within the last 120s from scrubbing.

**Option C**: Remove the scrub from the replenish hot path. Run it only in
`rebalance_pool` (manual/scheduled) instead of every replenish call.

## Impact

- Desktop assistants fail to get VMs under concurrent load (>5 simultaneous)
- The 5 idle VMs get assigned; remaining assistants wait indefinitely
- Container assignment works fine — only VM assignment is affected
- Production impact: users hiring desktop assistants during traffic spikes
  may see "no VM available" delays

## Related

- Julia's commit `8622bd6` introduced the scrub function to fix ghost VMs
- The ghost VM problem is real (VMs that fail to stop properly), but the
  fix is too aggressive and creates a new race
