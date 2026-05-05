# Qualifications and Certifications — UK Compliance

This is the **marquee capability** for property-management clients like
ClientZeta.  The UK letting/management sector has a dense set of
date-bound certifications:

| Certification | Renewal | Body |
|---|---|---|
| Gas Safe Registration | Annual | Gas Safe Register |
| NICEIC Approved Contractor | Annual | NICEIC |
| ARLA Propertymark | Annual | Propertymark |
| RICS chartered status | Annual + CPD | RICS |
| NEBOSH General Certificate | 3 years | NEBOSH |
| IOSH Managing Safely | 3 years | IOSH |
| Asbestos Awareness | Annual | UKATA / IATP |
| Legionella (L8) Awareness | 2 years | RSPH |
| Right to Work check | Per-employee, refresh on visa expiry | Home Office |

## The marquee query

```python
result = await query_local_expiring_qualifications(
    days_ahead=90,
    location_id="loc-battersea",  # optional — scope to a portfolio
)
# Returns property-level rollup of operatives whose certs expire in the
# next 90 days, ready for the assistant to surface as a property
# manager's morning brief.
```

The assistant should suggest this query whenever the user asks any of:

- "Whose Gas Safe expires soon?"
- "Compliance posture for [property/portfolio]?"
- "Who needs to renew certs this month/quarter?"
- "Any operatives I shouldn't dispatch to gas jobs right now?"

The `query_local_expiring_qualifications` function joins
`EmploymentHero/Qualifications/EmployeeRecords` with the Catalogue,
Employees, and Locations tables — that join requires the workforce +
qualifications syncs to have run.

## Live vs. local

- For "right now what's expiring" — `query_local_expiring_qualifications`
  is fine (cadence is 12 hours by default, so worst case the data is
  half a day stale).
- For "I just uploaded a new cert, does it look right?" — use live
  `list_employee_qualifications(employee_id=...)`.

## Tracking remediation

When a cert is expiring, the natural follow-up is "what training is
assigned to renew it?" — the `learning` capability covers that.  Join
`EmploymentHero/Qualifications/EmployeeRecords` with
`EmploymentHero/Learning/Assignments` on `employee_id` to see whether
the operative already has a renewal course assigned.
