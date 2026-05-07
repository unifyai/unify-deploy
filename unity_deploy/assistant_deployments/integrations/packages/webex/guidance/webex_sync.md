# Webex — Sync Runbook

`run_webex_sync_tick` is the entrypoint the scenario runtime calls on a
60s scheduler tick.  Per-object cadence is gated by env var.

## Object cadences (defaults)

| Object | Default interval |
|---|---|
| `meetings` | 15 min |
| `recordings` | 30 min |
| `transcripts` | 1 hr |
| `rooms` | 6 hr |
| `people` | 24 hr |

Override via `WEBEX_SYNC_OBJECT_INTERVALS=meetings:300,recordings:600,...`
(comma-separated `key:seconds`).  Defaults merge with overrides.

## Watermarks

Each successful sync writes to `Webex/Meta/SyncState`:

```json
{"object_type": "meetings", "last_synced_at": "...", "last_status": "ok"}
```

Subsequent ticks pass the watermark as `since=...`.  `/v1/meetings`,
`/v1/recordings`, and `/v1/meetingTranscripts` accept a `from` ISO
filter.

## Audit log

Every tick appends a row to `Webex/Meta/SyncRuns` with row totals,
errors, skipped reasons (`cadence_not_due` / `tier_gated_403`), and a
config snapshot.  Use `get_webex_sync_state()` to inspect.

## Manual recovery

```
run_webex_sync_tick(full=true, mock=false)   # replay from scratch
probe_webex_tier(force=true, mock=false)     # re-check what's gated
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tier_gated_403` on transcripts | App lacks `meeting:transcripts_read` | Add scope at developer.webex.com, Reconnect |
| `reconnect_required` envelope | Refresh token expired/revoked/rotated | User clicks Reconnect |
| Empty `webex_people` table | Connecting user lacks org-admin scope, or `/v1/people` requires a filter | Connect with an admin user, or rely on people surfaced via meeting invitees / room memberships |
| Transcripts present but `body` is null | `WEBEX_MIRROR_TRANSCRIPTS` was off during the producing sync | Set the env var to `true` and re-sync |
