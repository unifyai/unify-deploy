# Salesforce integration

Generic Salesforce CRM connector. Standard objects (Accounts, Contacts,
Leads, Opportunities, Cases) are first-class; everything else is reachable
through `run_salesforce_soql` and `describe_salesforce_object`.

OAuth 2.0 web-server flow against `login.salesforce.com`. Sandbox /
My Domain logins are not supported in v0.

See `guidance/salesforce_setup.md` for the customer-facing setup walkthrough.
