# Employees, Employments, Positions

## Common queries

```python
# Find an employee by name (live)
await search_employees(query="Alex Example", mock=False)

# List active employees in a team (local — preferred)
await query_local_employees(team_id="team-1", status="active", mock=False)

# Inspect employment + position history (live)
await list_employments("emp-1", mock=False)
await list_positions("emp-1", mock=False)
await list_managers("emp-1", mock=False)
```

## Structure

The synced `EmploymentHero/Employees` table is the workforce foundation.
Most analytical joins land here:

- `employees.team_id` → `EmploymentHero/Teams`
- `employees.location_id` → `EmploymentHero/Locations`
- `employees.manager_id` → `EmploymentHero/Employees` (self-join)

Employment history (`Employments`) and position history (`Positions`) are
separate tables joinable on `employee_id`.

## High-tier personal details

`list_emergency_contacts`, `list_dependants`, `get_visa_details`,
`get_probation_status` — see `sensitive_data.md`.  These are read-only
in v1; updates happen in EH directly.
