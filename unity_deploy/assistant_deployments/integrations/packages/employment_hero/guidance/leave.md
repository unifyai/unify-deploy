# Leave Management

## Categories (UK-typical)

EH ships standard categories: Annual Leave, Sick Leave (SSP), Maternity
(SMP), Paternity (SPP), Parental, Bereavement, Unpaid.  The org may
also define custom ones (Compassionate, Volunteering, etc.).

## Common queries

```python
# What categories does this org have?
await query_local_categories(mock=False)  # via list_leave_categories

# Pending leave for a specific employee
await query_local_leave_requests(employee_id="emp-1", status="pending", mock=False)

# All approved leave overlapping next month (for shift coverage planning)
await query_local_leave_requests(
    status="approved",
    from_date="2026-05-01",
    to_date="2026-05-31",
    mock=False,
)

# Current balance for an employee
await get_leave_balance("emp-1", mock=False)
```

## Submitting (HIGH-STAKES)

```python
await submit_leave_request(
    employee_id="emp-1",
    category_id="lc-1",          # Annual Leave
    start_date="2026-05-12",
    end_date="2026-05-16",
    notes="Family wedding",
    confirm=True,                 # only after user confirmation
    mock=False,
)
```

Gated by `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES`.  Always restate
parameters to the user before calling with `confirm=True`.

## Coverage planning across teams

For "who's off in my team next week" / shift planning, prefer the
synced copy — it joins naturally with `EmploymentHero/Employees` and
`EmploymentHero/Teams` for filtering.
