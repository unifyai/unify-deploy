# Infrastructure Integration Tests

End-to-end tests that run against real staging K8s, GCE, and Pub/Sub infrastructure. These tests exercise the exact same code paths as production, using real assistant data from Orchestra.

## What These Tests Prove

- **Container lifecycle**: idle containers can claim startup events, set correct K8s labels, and transition to live
- **Cleanup safety**: the `resource_version` guard prevents stale deletes; cleanup never kills live containers
- **Crash recovery**: the job-watcher detects pod terminations and runs cleanup
- **VM lifecycle**: pool VMs can be assigned, authenticated, and released correctly
- **Invariant health**: 14 infrastructure invariants are checked for violations
- **Duplicate prevention**: verifies whether concurrent startups produce split-brain (currently a known bug)
- **Task activation user flows**: scheduled tasks wake assistants quietly, running assistants accept due tasks in-place, trigger candidates piggyback on real inbound traffic, and offline tasks stay invisible to the live runtime lane

## Prerequisites

1. **GCP authentication**:
   ```bash
   gcloud auth login
   gcloud container clusters get-credentials unity --region us-central1 --project gcp-project-runtime
   ```

2. **Python environment**:
   ```bash
   pip install uv
   uv venv --python python3.12 .venv
   source .venv/bin/activate
   uv pip install -e ".[dev]"
   uv pip install pytest pytest-asyncio
   ```

3. **Test configuration**:
   ```bash
   cp tests/communication/infra/integration/.env.example tests/communication/infra/integration/.env
   # Edit .env and fill in your API keys
   ```

   Required keys:
   - `UNIFY_KEY` -- your personal API key (from console.unify.ai or your unity `.env`)
   - `ORCHESTRA_ADMIN_KEY` -- admin key for the Comms App (shared team key)
   - `ORCHESTRA_ADMIN_KEY` -- admin key for AssistantJobs system project

   Optional:
   - `TEST_ASSISTANT_ID` -- staging assistant ID to use. If not set, the first assistant found for your user is used automatically.
   - `TEST_GOOGLE_APPLICATION_CREDENTIALS` -- absolute path to a service-account JSON file for Pub/Sub checks
   - `TEST_GCP_SA_KEY` -- inline JSON for the same credential, if you prefer not to use a file

   Pub/Sub integration checks resolve credentials in this order:
   1. `TEST_GCP_SA_KEY`
   2. `GCP_SA_KEY`
   3. `TEST_GOOGLE_APPLICATION_CREDENTIALS`
   4. `GOOGLE_APPLICATION_CREDENTIALS`
   5. ambient ADC via `google.auth.default()`

## Running

```bash
# Full suite (~10-12 minutes)
pytest tests/communication/infra/integration/ -v -s

# Quick smoke test: invariant checker only (~50 seconds)
pytest tests/communication/infra/integration/test_invariant_checker.py -v

# Just lifecycle tests (~2 minutes)
pytest tests/communication/infra/integration/test_lifecycle.py -v -s

# Just cleanup safety (~2 minutes)
pytest tests/communication/infra/integration/test_cleanup_safety.py -v

# Just VM tests (~3 minutes)
pytest tests/communication/infra/integration/test_vm_lifecycle.py -v -s

# Just task activation user flows (~8-10 minutes)
pytest tests/communication/infra/integration/test_task_activation_flows.py -v -s
```

Note: `TEST_ORCHESTRA_URL` must be set to the staging Orchestra URL (not localhost):
```bash
export TEST_ORCHESTRA_URL="https://internal.example.com/v0"
```

Or add it to your `.env` file.

## Test Files

| File | Tests | What it covers | Invariants |
|------|-------|---------------|------------|
| `test_invariant_checker.py` | 4 | Full invariant health check, pool capacity | All 14 |
| `test_lifecycle.py` | 3 | Idle job creation, startup transition, pool capacity | INV-1,2,3,5,6 |
| `test_cleanup_safety.py` | 3 | resource_version guard, live container preservation | INV-7,8 |
| `test_duplicate_prevention.py` | 2 | Concurrent startups, label verification | INV-1,2 |
| `test_crash_recovery.py` | 2 | Pod kill recovery, orphan detection | INV-13,14 |
| `test_vm_lifecycle.py` | 4 | VM assign, auth probe, release, pool capacity | INV-9,10,11,12 |
| `test_stale_state.py` | 3 | Stale AssistantJobs records, is_job_running dead zone | INV-13 |
| `test_concurrency.py` | 4 | Burst startups, cleanup TOCTOU, pool exhaustion, rapid restart | INV-1,5,8 |
| `test_cross_service_contracts.py` | 3 | Label string contract, inventory accuracy, live count | INV-2,6 |
| `test_task_activation_flows.py` | 4 | Scheduled cold start, scheduled live delivery, trigger surfacing, offline invisibility | Product flow |

## How Tests Work

Each test that triggers a lifecycle transition (startup, crash, VM assign):
1. Fetches real assistant data from Orchestra (same admin endpoint the adapter uses)
2. Calls the real Comms App staging endpoints (same path as production)
3. Polls K8s/GCE for the expected state change
4. Cleans up created resources in a `finally` block
5. Triggers pool replenishment to replace consumed idle containers

The invariant checker runs as a warning after every test, catching any state drift the test caused.

## Troubleshooting

**Tests hang waiting for idle containers**: The staging pool may be exhausted from previous test runs. Run:
```bash
source .env
curl -X POST "$TEST_ADAPTERS_URL/scheduled/jobs/create" -H "Authorization: Bearer $ORCHESTRA_ADMIN_KEY"
```

**GCE tests skip**: Your `gcloud` auth needs access to the `gcp-project-vms` project. Verify with:
```bash
gcloud compute instances list --project=gcp-project-vms --zones=us-central1-a --limit=1
```

**Pub/Sub checks fail with `pubsub.subscriptions.consume`**: Your current ADC principal cannot pull from the staging outbound subscription. Set `TEST_GOOGLE_APPLICATION_CREDENTIALS` (or `TEST_GCP_SA_KEY`) to a credential that has `roles/pubsub.subscriber` on `gcp-project-runtime`.

**Orchestra calls fail with connection refused**: Make sure `TEST_ORCHESTRA_URL` points to staging, not localhost:
```bash
export TEST_ORCHESTRA_URL="https://internal.example.com/v0"
```

## Invariants

The test suite is organized around **invariants** -- properties that must hold at all times for the infrastructure to be correct. Each invariant maps to a specific user-visible failure if violated.

| ID | Property | If violated |
|----|----------|-------------|
| INV-1 | At most one container per assistant | Duplicate responses, split-brain voice calls |
| INV-2 | Live containers have correct labels | Invisible to cleanup and pool sizing |
| INV-3 | Idle containers have no assistant-id | False positive on is_job_running, message loss |
| INV-5 | Idle pool has capacity | Cold-start delays for users |
| INV-6 | Pool sizing reflects demand | Burst capacity starvation |
| INV-7 | Running containers have current image | Stale code after deploy |
| INV-8 | Cleanup never deletes live containers | Message loss |
| INV-9 | Desktop containers have assigned VM | Desktop/browser broken |
| INV-10 | Assigned VMs have live containers | VM pool capacity leak |
| INV-11 | VM auth key matches container key | 401 errors, desktop broken |
| INV-12 | VM idle pool has capacity | Desktop assignment delays |
| INV-13 | No orphaned AssistantJobs records | Message loss from stale records |
| INV-14 | No orphaned VM assignments | VM pool capacity leak |

When adding tests, always ask: "which invariant does this test verify?" If a test doesn't map to an existing invariant, consider whether a new invariant should be defined and documented in this table.

## Adding New Tests

1. Use the `comms` fixture for Comms App HTTP calls, `batch_api` for K8s, `gce_client` for GCE
2. Use `real_assistant_data` and `start_real_job()` for realistic startup events
3. Use `job_tracker` to auto-cleanup created Jobs
4. Use `poll()` instead of `sleep()` for waiting on async state changes
5. Mark which invariant(s) the test covers: `@pytest.mark.invariant("INV-N")`
6. New invariants: if you discover a property that must hold but isn't in the table above, add it to the table in this README, add the check to `check_invariants()` in `conftest.py`, and reference it in your test
