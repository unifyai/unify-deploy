# Qualifications and Certifications

Many industries — construction, healthcare, property management,
financial services, logistics — track date-bound certifications against
employees.  EH's qualifications module holds these as records keyed by
`(employee_id, qualification_id)` with `expires_at` and
`certificate_number` fields.

## The expiry-rollup query

```python
result = await query_local_expiring_qualifications(
    days_ahead=90,
    location_id=None,  # optional — scope to one site/portfolio
)
# Returns location-level rollup of employees whose certs expire in the
# given window, ready to surface as a manager's morning brief.
```

This function joins
`EmploymentHero/Qualifications/EmployeeRecords` with the Catalogue,
Employees, and Locations tables — that join requires the workforce +
qualifications syncs to have run.

The assistant should suggest this query whenever the user asks any of:

- "Whose [cert] expires soon?"
- "Compliance posture for [site / location]?"
- "Who needs to renew certs this month/quarter?"
- "Any operatives I shouldn't dispatch right now?"

Which qualifications matter (and their renewal cadence) depends on the
customer's vertical and jurisdiction — populate
`EmploymentHero/Qualifications/Catalogue` from the customer's EH instance
and reference it in scenario or client-package guidance.

## Live vs. local

- "What's expiring this quarter" — `query_local_expiring_qualifications`
  is fine (default cadence is 12 hours).
- "I just uploaded a new cert, does it look right?" — live
  `list_employee_qualifications(employee_id=...)`.

## Tracking remediation

When a cert is expiring, the natural follow-up is "what training is
assigned to renew it?" — the `learning` capability covers that.  Join
`EmploymentHero/Qualifications/EmployeeRecords` with
`EmploymentHero/Learning/Assignments` on `employee_id` to see whether
the operative already has a renewal course assigned.

## Read-only personal credentials

`get_visa_details`, `list_emergency_contacts`, `list_dependants`, and
`get_probation_status` live alongside qualifications in EH.  These are
read-only in v1; updates happen in EH directly.  See
`employmenthero_sensitive_data.md` for handling.
