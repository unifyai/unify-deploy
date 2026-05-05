# Onboarding

High turnover at front-line property roles means high onboarding pipeline
throughput.  Tracking outstanding tasks per new starter is core utility.

## What's tracked

| Table | Shape |
|---|---|
| `EmploymentHero/Onboarding/Processes` | Onboarding flow definitions (Maintenance Operative, Property Manager, etc.) |
| `EmploymentHero/Onboarding/Tasks` | Task templates (RTW check, Gas Safe upload, NDA signed, etc.) |
| `EmploymentHero/Onboarding/EmployeeStatus` | Per-employee task completion |

## Common queries

```python
# Outstanding tasks for one new hire
await query_local_onboarding_status(
    employee_id="emp-new-1", status="outstanding", mock=False,
)

# Org-wide outstanding tasks (the new-hire backlog)
await query_local_onboarding_status(status="outstanding", mock=False)

# Definition lookup
await list_onboardings(mock=False)
await list_onboarding_tasks(onboarding_id="ob-maintenance", mock=False)
```

## "How are my new hires doing?"

The assistant should default to:

```sql
SELECT e.first_name, e.last_name, e.start_date,
       COUNT(*) FILTER (WHERE eos.status = 'completed') AS done,
       COUNT(*) AS total
FROM   EmploymentHero/Employees e
JOIN   EmploymentHero/Onboarding/EmployeeStatus eos ON eos.employee_id = e.id
WHERE  e.start_date >= DATE('now', '-90 days')
GROUP BY e.id
ORDER BY e.start_date DESC;
```

This gives the property manager their new-hire onboarding completion
rollup without listing every task.
