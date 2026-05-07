# Recognition and Surveys

## Recognition

Three event types: **cheers** (small acknowledgements), **hi-fives**
(peer kudos), **awards** (formal recognition).  All synced.  Giving
recognition (`give_cheer`, `give_hi_five`) is high-stakes — see
`employmenthero_high_stakes_writes.md`.

## Surveys — anonymity

Anonymity is **driven by the EH `is_anonymous` flag on the survey
definition**.  The package respects this:

| Survey flag | Synced storage |
|---|---|
| `is_anonymous=true` | Aggregates only (`EmploymentHero/Surveys/Aggregates`) — no per-respondent rows |
| `is_anonymous=false` | Per-respondent rows in `EmploymentHero/Surveys/Responses` with free-text answers redacted to length+hash |

`EMPLOYMENTHERO_SURVEYS_FORCE_ANONYMOUS=true` overrides to treat ALL
surveys as anonymous (extra-conservative mode).

## Anti-patterns

- For anonymous surveys, never attempt to deanonymise via timing,
  free-text fingerprinting, or correlation across surveys.  The package
  never stores plaintext for anonymous surveys, but the assistant should
  also refuse correlation requests.
- Don't sum aggregates across mismatched populations (e.g. mixing two
  surveys' means on the same chart).
