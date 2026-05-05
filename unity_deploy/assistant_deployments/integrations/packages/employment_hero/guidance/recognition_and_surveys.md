# Recognition and Surveys

## Recognition

Three event types: **cheers** (small acknowledgements), **hi-fives**
(peer kudos), **awards** (formal recognition).  All synced.

```python
await list_cheers(employee_id="emp-1", mock=False)
await list_hi_fives(employee_id="emp-1", mock=False)
await list_recognition_awards(mock=False)
```

### Giving (HIGH-STAKES)

```python
await give_cheer(
    to_employee_id="emp-1",
    value="Above and beyond",
    message="Stayed late to handle the Camden boiler emergency.",
    confirm=True,
    mock=False,
)

await give_hi_five(to_employee_id="emp-1", confirm=True, mock=False)
```

## Surveys — anonymity

Anonymity is **driven by the EH `is_anonymous` flag on the survey
definition**.  The package respects this:

| Survey flag | Synced storage |
|---|---|
| `is_anonymous=true` | Aggregates only into `EmploymentHero/Surveys/Aggregates` (mean, count, etc.) — no per-respondent rows |
| `is_anonymous=false` | Per-respondent rows in `EmploymentHero/Surveys/Responses` with free-text answers redacted to length+hash |

`EMPLOYMENTHERO_SURVEYS_FORCE_ANONYMOUS=true` overrides to treat ALL
surveys as anonymous (extra-conservative mode).

## Common queries

```python
# Survey definitions
await list_surveys(mock=False)

# Aggregated responses for an anonymous survey
await list_survey_responses("sv-1", mock=False)
```

## Anti-patterns

- For anonymous surveys, never attempt to deanonymise via timing,
  free-text fingerprinting, or correlation across surveys.  The
  package never stores plaintext for anonymous surveys, but the
  assistant should also refuse correlation requests.
- Don't sum aggregates across mismatched populations (e.g. mixing two
  surveys' means on the same chart).
