# Learning

Mandatory training often interlocks with the qualifications expiry flow
— a course completion can renew a qualification.

## Tables

| Table | Shape |
|---|---|
| `EmploymentHero/Learning/Courses` | Catalogue (name, duration, mandatory flag, category) |
| `EmploymentHero/Learning/Assignments` | Per-employee course assignments with due dates |
| `EmploymentHero/Learning/Completions` | Per-employee completion records with score |

## Tying to qualifications

For "who has cert X expiring AND no renewal course assigned":

```sql
SELECT e.first_name, e.last_name, eq.expires_at
FROM   EmploymentHero/Qualifications/EmployeeRecords eq
JOIN   EmploymentHero/Employees e ON e.id = eq.employee_id
LEFT JOIN EmploymentHero/Learning/Assignments la
       ON la.employee_id = e.id
      AND la.course_id = '<renewal-course-id>'
WHERE  eq.qualification_id = '<cert-id>'
   AND eq.expires_at <= DATE('now', '+90 days')
   AND la.id IS NULL;
```

Renewal-course IDs depend on the customer's catalogue.  When the
customer doesn't have a dedicated learning module in EH, training may
live in a separate integration (e.g. an LMS connector); fall back to
qualifications-only queries.
