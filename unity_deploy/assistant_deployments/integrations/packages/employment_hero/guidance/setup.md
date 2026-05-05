# Setup — Employment Hero Connection

## Required secrets

| Secret | Required | What |
|---|---|---|
| `EMPLOYMENTHERO_ACCESS_TOKEN` | for live calls | Bearer token from EH developer portal |
| `EMPLOYMENTHERO_ORGANISATION_ID` | when token has access to >1 org | Pin the active organisation |
| `EMPLOYMENTHERO_BASE_URL` | UK customers if regional URL needed | Defaults to `https://api.employmenthero.com` |

The customer adds these via Console → Settings → Secrets.  Without
`EMPLOYMENTHERO_ACCESS_TOKEN` set, every read function returns a
structured "not configured" envelope and the sync orchestrator skips.

## First-time setup checklist

1. Customer generates a private bearer token in the EH developer
   portal (Settings → Integrations → API).
2. Customer pastes it into Console → Secrets as
   `EMPLOYMENTHERO_ACCESS_TOKEN`.
3. Assistant calls `get_account_info(mock=False)` to verify the token
   resolves a user + organisation.
4. If the response includes a `_hint` about pinning the active
   organisation, the user copies the suggested
   `EMPLOYMENTHERO_ORGANISATION_ID` value into Secrets.
5. Run `probe_tier(force=true)` to discover available capabilities.
6. (Optional) Operator flips `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES`
   if writes should be permitted.
7. Run a manual sync via `run_employmenthero_sync_tick(full=true, mock=false)`.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `EMPLOYMENTHERO_ACCESS_TOKEN is not configured.` | Secret not set | Add via Console |
| `Token rejected (401)` | Token revoked or expired | Regenerate in EH portal |
| `403` on specific capabilities | Token scope or EH plan tier | See `tier_gating.md` |
| `Multiple organisations may be available` hint | Token has access to >1 org | Set `EMPLOYMENTHERO_ORGANISATION_ID` |
| `429` rate-limit | Sync too aggressive | Lower `EMPLOYMENTHERO_API_PAGE_SIZE` or increase `EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR` |
