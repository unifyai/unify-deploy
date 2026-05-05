# Expenses

## Common queries

```python
# An employee's last 30 days of claims (local)
await query_local_expenses(
    employee_id="emp-2",
    from_date="2026-04-01",
    mock=False,
)

# Live single-claim fetch
await get_expense_claim("ex-1", mock=False)

# Org-wide submitted claims awaiting approval
await query_local_expenses(status="submitted", mock=False)
```

## Categories

UK-typical: Travel - Mileage, Materials, Subsistence, Accommodation,
Phone, Office Supplies, plus org-defined ones (e.g. ClientZeta-specific
maintenance materials sub-categories).

```python
await list_expense_categories(mock=False)
```

## Submitting (HIGH-STAKES)

```python
await submit_expense_claim(
    employee_id="emp-2",
    category_id="ec-1",  # Travel - Mileage
    amount=12.40,
    currency="GBP",
    date="2026-04-28",
    description="Mileage Battersea -> Camden site visit",
    confirm=True,
    mock=False,
)
```

For UK mileage claims, the assistant should check if the org uses HMRC
approved mileage rates (typically 45p/mile for first 10k business miles,
25p thereafter).
