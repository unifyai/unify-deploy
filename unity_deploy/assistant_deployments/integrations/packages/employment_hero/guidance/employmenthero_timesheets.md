# Timesheets

## Common queries

```python
# A specific employee's last week of timesheets (local — preferred)
await query_local_timesheets(
    employee_id="emp-2",
    from_date="2026-04-23",
    to_date="2026-04-30",
    mock=False,
)

# Live single-entry fetch
await get_timesheet_entry("ts-1", mock=False)

# Submitted entries awaiting approval, org-wide
await query_local_timesheets(status="submitted", mock=False)
```

## Submitting / updating (HIGH-STAKES)

```python
await submit_timesheet_entry(
    employee_id="emp-2",
    date="2026-04-29",
    start_time="08:00",
    end_time="17:00",
    hours=8.5,
    project="Battersea Portfolio",
    notes="Routine maintenance — Flat 14",
    confirm=True,
    mock=False,
)
```

## Overtime and missed-submission patterns

The synced `EmploymentHero/Timesheets` retains a rolling 14-day window
by default (controlled by `since=` watermarking in the orchestrator).
For "any maintenance operative racking up overtime this week?":

```sql
SELECT employee_id, SUM(hours) AS total
FROM   EmploymentHero/Timesheets
WHERE  date >= DATE('now', '-7 days')
GROUP BY employee_id
HAVING total > 48;
```

For missed-submission: join `EmploymentHero/Employees` (active) with
`EmploymentHero/Timesheets` for the past N days; flag employees with
zero rows on workday-flagged dates.
