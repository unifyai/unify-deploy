# HubSpot Analytics

Three surfaces: CRM analytics, email analytics, custom reports.

## CRM Analytics

- `get_deal_velocity_report(pipeline_id, period_days)` - close rate, average
  days to close, time per stage.  Useful when the user asks "how's our
  pipeline doing" or "what's our average deal cycle".
- `get_pipeline_funnel_report(pipeline_id)` - current deal count + dollar
  amount per stage.  Useful for "where are deals stuck?"

In v0 these are mock-only.  Production aggregates from the synced
`HubSpot/CRM/Dimensions/Deals` and `HubSpot/CRM/Facts/DealStageHistory`
contexts in DataManager.

## Email Analytics

- `get_email_performance(email_id)` - per-email open / click / bounce rates.
- `list_email_event_summary(days=...)` - rolling window summary.

## Custom Reports

`list_crm_reports` / `run_crm_report` - tier-gated.  Useful when a customer
says "show me the Q3 owner acquisition report" - find the report by name,
then run it.

## When to choose what

- Specific question about a single metric → call the targeted report.
- Open-ended "how are we doing" → `get_pipeline_funnel_report` +
  `get_deal_velocity_report` together give a strong narrative.
- Custom-named report the user mentions → `list_crm_reports` to find by
  name, then `run_crm_report`.
