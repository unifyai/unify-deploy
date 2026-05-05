# Learning

Mandatory training (Fair Housing & Equality, Health & Safety, GDPR,
asbestos awareness) often interlocks with the qualifications expiry
flow — a course completion can renew a qualification.

## What's tracked

| Table | Shape |
|---|---|
| `EmploymentHero/Learning/Courses` | Catalogue (name, duration, mandatory flag, category) |
| `EmploymentHero/Learning/Assignments` | Per-employee course assignments with due dates |
| `EmploymentHero/Learning/Completions` | Per-employee completion records with score |

## Common queries

```python
# Course catalogue
await list_courses(mock=False)

# Outstanding mandatory training for an employee
await list_course_assignments(employee_id="emp-1", mock=False)

# Recent completions across the org
await list_course_completions(mock=False)
```

## Tying to qualifications

For "who has Gas Safe expiring AND no renewal course assigned":

```sql
SELECT e.first_name, e.last_name, eq.expires_at
FROM   EmploymentHero/Qualifications/EmployeeRecords eq
JOIN   EmploymentHero/Employees e ON e.id = eq.employee_id
LEFT JOIN EmploymentHero/Learning/Assignments la
       ON la.employee_id = e.id
      AND la.course_id = 'course-gas-safe-renewal'
WHERE  eq.qualification_id = 'qual-gas-safe'
   AND eq.expires_at <= DATE('now', '+90 days')
   AND la.id IS NULL;
```
