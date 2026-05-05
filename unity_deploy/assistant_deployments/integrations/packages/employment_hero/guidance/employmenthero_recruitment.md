# Recruitment

**Tier-2 sensitive** — applicant PII is redacted at snapshot time.
Read-only in v1.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS` | `180` | Only sync applicants whose `updated_at` is within this window |
| `EMPLOYMENTHERO_RECRUITMENT_REDACT_PII` | `true` | Redact first/last name + email + phone in synced rows |
| `EMPLOYMENTHERO_RECRUITMENT_INCLUDE_REJECTED` | `false` | Skip applicants in `rejected` stage |

The retention window keeps DataManager from accumulating stale
applicant data indefinitely.  The PII redaction lets analytics work
("how many candidates per stage per quarter") without persisting
identifiable applicant data.

## What's tracked

| Table | Notes |
|---|---|
| `EmploymentHero/Recruitment/Jobs` | Open positions (title, location, status) |
| `EmploymentHero/Recruitment/Applicants` | Candidates; PII redacted |
| `EmploymentHero/Recruitment/Offers` | Offers extended; respond status |
| `EmploymentHero/Recruitment/InterviewStages` | Stage definitions for the org |

## Common queries

```python
# Open jobs
await list_jobs(status="open", mock=False)

# Applicants for a specific job
await list_applicants(job_id="job-1", mock=False)

# Offers extended (live)
await list_offers(mock=False)
```

## What the assistant won't do

- Move applicants between stages.  All applicant decisions stay in EH.
- Accept / decline offers on behalf of the user.  The candidate
  responds through EH directly.
- Send communications to applicants from the assistant.

## Reading individual applicants live

`get_applicant(applicant_id, mock=False)` returns the full record
including PII (the redaction is at snapshot time, not at the API).
The assistant should ask the user to confirm authority (hiring manager
/ HR) before pulling individual applicant detail.
