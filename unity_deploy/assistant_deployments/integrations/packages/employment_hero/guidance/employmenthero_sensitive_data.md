# Employment Hero — Sensitive Data Handling

Three sensitivity tiers govern every interaction with EH data.

## Tier classification

| Tier | Examples | Storage | Display |
|---|---|---|---|
| **Normal** | Names, positions, leave dates, timesheet hours, locations, qualification names, goal progress, recognition events | Synced as-is | Surfaced in responses normally |
| **High** | Performance review free-text, 1:1 notes, feedback bodies, peer feedback, employee HR notes, emergency-contact names + phones, dependants, visa details, applicant PII | Synced with **field-level redaction** (length+hash); live API returns full content | Manager/owner authorisation required; confirm intent before reading |
| **Critical** | Payslips, banking details, sort codes, account numbers, tax declarations including UK NI numbers and AU TFNs, medical disclosures, exact pay rates | **Never synced**; live API only with masked returns | Confirm `confirm_user_authorised=True` only after explicit user confirmation; never echo full values |

## Refusal patterns

When the user asks for high-tier or critical-tier data:

1. **Restate what they're asking for in plain English** so they can
   sanity-check before authorising.
2. **Confirm authority** — for high-tier: are they the manager / owner?
   For critical-tier: are they the payroll admin or the employee
   themselves?
3. **Wait for an explicit yes** in the chat (not implied from prior
   context).
4. **Call the function with the explicit gate** — `confirm_user_authorised=True`
   for critical-tier, or follow the `confirm=True` pattern for writes.
5. **For critical-tier values, summarise — never echo verbatim.**
   Examples:
   - "Their bank account ends ****1234 at Example Bank UK."
   - "An NI number is on file."
   - "Their tax code is 1257L; the NI number is recorded but not
     displayed here — view it in Employment Hero directly."
6. If the user pushes for the full value, refuse and tell them to view
   it in Employment Hero.

## Redaction details

High-tier sync replaces free-text with `{field}_length` + `{field}_hash`
(SHA-256 first 16 chars).  This lets the assistant detect duplicates,
verify counts, or correlate across records — without ever holding the
plaintext content in DataManager.

If the user asks for the full text of a redacted field (e.g. a review's
self-assessment), call the live `get_review()` function — that returns
the full content from the EH API, scoped to the user's session.

## GDPR

ClientZeta is UK-based and GDPR applies.  Two implications:

- **Minimise persistence.**  We respect this by never syncing critical
  data and by redacting high-tier data at the snapshot boundary.
- **Subject access requests.**  If asked to support an SAR, the
  assistant should produce a list of contexts under
  `EmploymentHero/...` that contain rows for the subject employee, not
  attempt to export their data — actual SAR fulfilment is via the
  Employment Hero admin tools.
