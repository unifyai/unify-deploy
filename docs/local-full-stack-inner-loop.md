# Full Local Stack Inner Loop

This runbook is for internal development across the sibling `unity`, `console`,
and `orchestra` checkouts. Public `unity` users do not have this private repo;
the public README correctly describes running `unity` against the hosted
Orchestra backend.

## Default Workflow

Start the full source stack once:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh up
```

Then make code changes in the sibling repos. Console changes hot reload through
the stack-owned Next dev server; Unity and Orchestra changes should be exercised
through the already-running local services unless the specific code path needs a
process restart.

Check what is running before changing local services:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh status
```

Repair only Console when Next chunks or environment get out of sync:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh repair-console
```

Run a product smoke check:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh smoke
```

## Cursor Agent Startup

When a Cursor agent starts the full source stack for someone else to use, it must
use the durable launcher:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh up --durable
```

The durable launcher owns a `tmux` session named `unity-stack`, waits for the
stack-ready marker, verifies Console over HTTP, and prints attach/stop commands.
This keeps Console, Orchestra, the Unity gateway, the Coordinator runtime, and
the managed call tunnel out of the Cursor shell job's process group, so they keep
running after the agent command exits. A plain `stack.sh up` is fine from a
long-lived human terminal, but agents should not emulate persistence with
`nohup`, backgrounded subshells, or sleep loops.

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
- `unity/scripts/local.sh start` or `start-gateway`
- Compose `unity up` while the source stack is running, or source `stack.sh up`
  while Compose self-host is running

The guardrails should explain the relevant override variable when isolated mode
is really needed.

## Console Environment

Print the non-secret Console environment expected by the full stack:

```bash
bash /Users/djl11/unity-deploy/selfhost/stack.sh dev-env
```

The important invariant is that Console is started by the harness with the same
ports and topology as the rest of the stack: `SELF_HOST=1`,
`ORCHESTRA_URL=http://127.0.0.1:8000`, gateway/adapters on `8001`, Pub/Sub on
`8085`, and LiveKit Cloud credentials from the self-host state file.

## Owner States

The source stack has two valid local ownership states:

- **Pre-signup infra-ready:** Console, Orchestra, Pub/Sub, gateway, and call
  tunnel are running, but there is no user, Coordinator, or CM runtime yet.
  Register in Console to create the single local owner.
- **Post-signup runtime-ready:** the UI-created owner has one personal
  Coordinator, and `~/.unity/coordinator-runtime.json` plus
  `~/.unity/self-host-owner.json` point at that same user/assistant pair.

No setup or reset path should create a placeholder owner. Duplicate Coordinator
rows are a repair condition; run `bash /Users/djl11/unity-deploy/selfhost/stack.sh
reset --yes` to preserve the real owner if one exists, or return to pre-signup
when no owner exists.
