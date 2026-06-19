# Full Local Stack Inner Loop

This runbook is for internal development across the sibling `droid`, `console`,
and `orchestra` checkouts. Public `droid` users do not have this private repo;
the public README correctly describes running `droid` against the hosted
Orchestra backend.

## Default Workflow

Start the full source stack once:

```bash
bash /Users/djl11/droid-deploy/selfhost/stack.sh up
```

Then make code changes in the sibling repos. Console changes hot reload through
the stack-owned Next dev server; Droid and Orchestra changes should be exercised
through the already-running local services unless the specific code path needs a
process restart.

Check what is running before changing local services:

```bash
bash /Users/djl11/droid-deploy/selfhost/stack.sh status
```

Repair only Console when Next chunks or environment get out of sync:

```bash
bash /Users/djl11/droid-deploy/selfhost/stack.sh repair-console
```

Run a product smoke check:

```bash
bash /Users/djl11/droid-deploy/selfhost/stack.sh smoke
```

## Safe Checks While The Stack Is Running

Use repo-specific checks that do not write production build artifacts into the
live app checkout:

```bash
cd /Users/djl11/console
npm run ci:live-safe
```

Run full production builds in a separate worktree or when the live Console dev
server is stopped. `next build` and `next dev` share `.next`; running both in
one checkout can leave the browser with missing chunks.

## Unsafe Commands

Do not run these against a live full stack unless you intentionally opt into an
isolated mode:

- `npm run dev`, `next dev`, `npm run build`, or `npm run ci` in `console`
- `console/scripts/local.sh start` without `--self-host`
- `orchestra/scripts/local.sh start`, `restart`, or `purge`
- `droid/scripts/local.sh start` or `start-gateway`
- Compose `droid up` while the source stack is running, or source `stack.sh up`
  while Compose self-host is running

The guardrails should explain the relevant override variable when isolated mode
is really needed.

## Console Environment

Print the non-secret Console environment expected by the full stack:

```bash
bash /Users/djl11/droid-deploy/selfhost/stack.sh dev-env
```

The important invariant is that Console is started by the harness with the same
ports and topology as the rest of the stack: `SELF_HOST=1`,
`ORCHESTRA_URL=http://127.0.0.1:8000`, gateway/adapters on `8001`, Pub/Sub on
`8085`, and LiveKit on `7880`.
