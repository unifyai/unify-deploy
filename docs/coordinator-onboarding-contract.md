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
