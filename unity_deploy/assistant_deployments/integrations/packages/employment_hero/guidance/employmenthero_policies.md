# Policies and Acknowledgements

## What's tracked

Each org-defined HR policy with version history.  Per-employee
acknowledgements track who has signed off on which version.

```python
# All current policies
await list_policies(mock=False)

# Acknowledgements for a specific policy
await list_policy_acknowledgements(policy_id="pol-1", mock=False)

# Acknowledgements for a specific employee
await list_policy_acknowledgements(employee_id="emp-1", mock=False)
```

## Compliance posture queries

For "who hasn't acknowledged Code of Conduct v3?":

```sql
SELECT e.first_name, e.last_name, e.position
FROM   EmploymentHero/Employees e
LEFT JOIN EmploymentHero/Policies/Acknowledgements a
       ON a.employee_id = e.id
      AND a.policy_id = 'pol-1'
      AND a.policy_version = '3'
WHERE  e.status = 'active'
   AND a.acknowledged_at IS NULL;
```

## Acknowledging (HIGH-STAKES)

```python
await acknowledge_policy(
    policy_id="pol-1",
    employee_id="emp-1",
    confirm=True,
    mock=False,
)
```

The assistant should usually only acknowledge policies on behalf of the
calling user themselves, not other employees, unless the user has HR
admin authority.
