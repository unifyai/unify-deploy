# Pay, Banking, Tax

**CRITICAL TIER.**  Refer to `sensitive_data.md` for the full refusal
patterns.  This page covers the practical workflows.

## Pay runs (`pay_runs`)

Pay runs return **org-level totals**, never per-employee figures.

```python
# Recent pay runs
await list_pay_runs(limit=12, mock=False)

# Specific run summary
await get_pay_run("pr-1", mock=False)

# Pay categories defined for the org
await list_pay_categories(mock=False)
```

The synced `EmploymentHero/Pay/Runs` table is safe to query — only
totals (gross, tax, NI, pension, net), not individuals.

## Employment terms (`employment_terms`)

Per-employee employment classification, rate type (salary/hourly), and
exact rate.  Rate is **banded** at snapshot time when
`EMPLOYMENTHERO_PAY_RATE_BANDS=true` (default).

```python
# Banded summary from the synced copy (safe for analytics)
await dm.filter("EmploymentHero/Pay/EmploymentTerms", filter="...")

# Exact rate (live — confirm authorisation)
await get_employment_terms("emp-1", confirm_user_authorised=True, mock=False)
```

The banded table answers questions like "how many maintenance roles are
in the £35-45k band" without revealing exact compensation.

## Payslips (`payslips`)

**Live-only, masked returns.**  Never synced.

```python
await list_employee_payslips(
    employee_id="emp-1",
    confirm_user_authorised=True,
    mock=False,
)
# Returns rows with gross/net/tax shown as "£***.**".
```

The user must view exact line items in Employment Hero directly.

## Banking (`banking_and_tax`)

**Live-only, tail-masked returns.**

```python
await get_banking_details(
    employee_id="emp-1",
    confirm_user_authorised=True,
    mock=False,
)
# Returns: account_holder_name="PRESENT", sort_code_masked="**-**-12",
#          account_number_masked="****1234", bank_name="Example Bank UK".
```

Sort codes and account numbers are tail-masked.  Full values must be
viewed in EH.

## Tax declarations

UK NI numbers and AU TFNs are returned as `PRESENT` / `MISSING` only.
Tax codes are returned in full (1257L, etc.) since they're not by
themselves identifying.

```python
await get_tax_declaration(
    employee_id="emp-1",
    confirm_user_authorised=True,
    mock=False,
)
```

## Patterns the assistant uses

- "Their bank record is on file at Example Bank UK; the account number
  ends ****1234."
- "There's an NI number on record (status: PRESENT). Exact value isn't
  accessible to me."
- "The April pay run cleared at £412,500 gross, £321,200 net across
  124 employees."
- "Their salary band is `<45,000 GBP`. Exact figure is available in
  Employment Hero."

## Refusal pattern when the user pushes for full values

> "I can confirm the value is on file but I won't display the full
> sort code / account number / NI number / pay rate.  These are
> critical-tier records — please view them in Employment Hero
> directly."
