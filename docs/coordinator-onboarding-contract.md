# Coordinator Onboarding Contract

Coordinator onboarding is a cross-repo flow. The contract below keeps the
product graph, UI behavior, and agent prompt behavior from drifting.

## Repository Ownership

- `orchestra` owns the onboarding graph in
  `orchestra/services/onboarding_graph.py`: phases, steps, dependencies,
  section framing, step nudges, interaction payloads, and event specs.
- `orchestra` also owns runtime projection in
  `orchestra/services/coordinator_service.py`: state derivation,
  `compute_onboarding_render`, and all graph-owned onboarding event emission.
- `console` owns rendering and local UI actions only: picker state, checklist
  rows, opening tabs/dialogs, and calling Orchestra endpoints when the user
  clicks a graph-owned event row.
- `unity` owns conversational policy: how Twin speaks the current contract over
  chat or voice, how long each turn class may be, and how live call text is
  shaped for TTS.
- `unity-deploy` owns this private full-local-stack contract documentation.
  Do not move full Console/Orchestra onboarding instructions into public
  `unity` docs.

## Section-Wide vs Step-Specific Copy

Section-wide guidance belongs on the Orchestra phase. Use it for why a section
exists and the shared interaction pattern across its steps.

Examples:

- Communication: prove channels with a reference quiz.
- Workspace: explain why Google/Microsoft access unlocks later setup.
- Tasks: explain task definitions vs one-off actions.

Step-specific guidance belongs on the Orchestra step. Use it for the concrete
row action, channel, tool, paired reply step, nudge copy, and interaction data.

Examples:

- `email-reference`: send the first reference-quiz clue by email.
- `email-reply`: wait for the user's guess by email.
- `workspace`: open the OAuth dialog.

Unity should not hardcode section game design, clue content, step ordering, or
Console click paths when Orchestra can provide them in the render.

## Event Contract

All graph-owned onboarding events must be emitted through Orchestra, not posted
directly from Console to Unity. The event payload must include:

- `event_type: coordinator_onboarding_event`
- stable `subtype`
- step-specific fields such as `step_id`, `trigger_step_id`, `reply_step_id`,
  `channel`, `tool_name`, and `interaction`
- section fields such as `phase_id`, `phase`, and `phase_framing`
- the current `onboarding` render attached under `details.onboarding`

The attached render is what makes Unity's prompt state current immediately after
the click. Without it, the agent can speak from stale progress and skip the
section framing that explains why the step exists.

## Step Completion: Derived vs Explicit

Most steps complete by *derivation* — Orchestra reads durable state and decides
a step is done:

- Communication reference-quiz steps derive from assistant-authored transcript
  rows on the channel's outbound mediums (see
  `_CHANNEL_TO_OUTBOUND_MEDIUMS`). Unity tags those outbounds with onboarding
  metadata so the row Orchestra reads is attributable.
- Connect/Tasks steps derive from the resulting account state (a workspace
  contact, an app connection, a Tasks row).

Workspace demos (`workspace-mailbox`, `workspace-drive`, `workspace-calendar`)
are the deliberate exception: they **never auto-complete**. The checklist does
not detect the demo work from any transcript row, so completion is always an
explicit brain action. Instead:

1. The user clicks the demo row → Orchestra emits `workspace_demo_requested`.
2. Twin performs the demo task with its own tools: read the relevant area and
   deliver one short summary as a single `unify_message` (for the mailbox,
   summarise the recent mail). Any reply, tidy-up, or flag Twin offers
   afterwards is an optional follow-up and never gates completion.
3. Twin marks the step done explicitly by calling `set_onboarding_task_state`,
   which PATCHes `onboarding_step_completion` on `/assistant/{id}/state`. The
   step id is recorded in `manually_completed_step_ids`. The demo is not
   finished until this call is made.
4. When that PATCH lands and onboarding is active, Orchestra emits
   `onboarding_step_completed` carrying the freshly-derived render. Unity
   refreshes its progress model from the attached render but does **not** run an
   extra acknowledgement turn — the brain already messaged the user on the turn
   that made the completion call.

`manual_completion_block_reason` in the graph is the single gate for which steps
accept an explicit completion PATCH. Communication rows and other auto-derived
triggers remain blocked; the three workspace demos are allowed.

## Prompt Precedence

Unity has three turn classes:

- `session_open`: may be a longer orientation. It introduces Twin, explains the
  onboarding walkthrough, names the active section's purpose, and proposes the
  first valid next target.
- `milestone_ack`: one short acknowledgement plus the next valid target.
- `interaction_event`: follows the structured interaction contract, such as a
  reference quiz clue.

For voice calls, active onboarding opening guidance overrides generic greeting
rules. A call with valid onboarding `next_targets` must not start with only
“how can I help?”

## Adding A Future Section

1. Add the phase and section framing in Orchestra.
2. Add steps with dependencies, nudges, presentation copy, flow notes, and any
   structured interaction fields.
3. Add or extend Orchestra tests for catalog, render, and event payloads.
4. Add Console row-action handling only if the step opens a UI surface or
   dispatches a graph-owned event endpoint.
5. Add Unity tests for how the new section is spoken, but do not duplicate the
   graph copy in Unity prompts.
6. Verify the flow through the full local stack in `unity-deploy/selfhost`.
