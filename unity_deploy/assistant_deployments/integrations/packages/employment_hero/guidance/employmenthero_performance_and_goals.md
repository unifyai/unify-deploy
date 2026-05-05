# Performance, Goals, 1:1s, Feedback

**Free-text content is high-tier sensitive — see `employmenthero_sensitive_data.md`.**
The synced copy redacts review feedback, 1:1 notes, peer feedback
bodies to length+hash.  Use live API for full content with appropriate
authorisation.

## Goals

Goals are **not redacted** (they're business KPIs).  Synced as full
records.

```python
# Active goals for a property manager
await query_local_goals(employee_id="emp-pm-1", status="in_progress", mock=False)

# Live single-goal fetch
await get_goal("goal-1", mock=False)
```

### Creating / updating (HIGH-STAKES)

```python
await create_goal(
    employee_id="emp-pm-1",
    title="Q2 occupancy >= 92% across Battersea Portfolio",
    target_value=92.0,
    unit="percent",
    due_date="2026-06-30",
    confirm=True,
    mock=False,
)

await update_goal_progress(
    goal_id="goal-1",
    current_value=89.2,
    note="EICR remediation cleared two voids; on track for Q2.",
    confirm=True,
    mock=False,
)
```

## Reviews

```python
# Review summary (synced — free-text redacted)
await query_local_reviews(employee_id="emp-pm-1", period="2026Q1", mock=False)

# Full content (live — high-tier)
await get_review("rev-1", mock=False)
```

## 1:1s

```python
# Schedule of upcoming 1:1s
await list_one_on_ones(employee_id="emp-pm-1", mock=False)

# Notes from one specific 1:1 (live)
await get_one_on_one("111-1", mock=False)
```

### Creating / annotating (HIGH-STAKES)

```python
await create_one_on_one(
    employee_id="emp-pm-1",
    manager_id="emp-mgr-1",
    scheduled_at="2026-05-07T15:00:00Z",
    duration_minutes=30,
    agenda_notes="Q2 goal alignment, BTL portfolio handover.",
    confirm=True,
    mock=False,
)
```

## Feedback

```python
await list_feedback(employee_id="emp-1", mock=False)
await list_peer_feedback("emp-1", mock=False)

# Giving feedback (HIGH-STAKES)
await give_feedback(
    subject_employee_id="emp-1",
    feedback_type="appreciation",
    body="Excellent work coordinating the Camden EICR rollout.",
    is_anonymous=False,
    confirm=True,
    mock=False,
)
```

## Anti-patterns

- Don't paste raw redacted hashes into responses.  If a hash is the
  best you have, summarise (e.g. "the review has manager feedback
  recorded; full content available via the live API").
- Don't bulk-summarise other employees' performance feedback unless
  the user is the manager-of-record or HR.
