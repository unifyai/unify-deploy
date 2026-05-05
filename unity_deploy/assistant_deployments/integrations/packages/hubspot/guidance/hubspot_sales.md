# HubSpot Sales Hub

Sequences, templates, snippets, documents, meeting links.  Sequences are
tier-gated (Sales Hub Pro+).

## Sequences

`enroll_in_sequence(sequence_id, contact_email)` starts a contact through a
multi-step outbound cadence.  `unenroll_from_sequence` cancels.  Use these
for systematic outreach (e.g., owner-acquisition campaigns) where the
sequence is configured by a human in HubSpot.

## Templates and snippets

- **Templates** (`sales_templates.py`) are reusable email bodies with
  personalization tokens like `{{contact.firstname}}`.  Read-only here -
  templates are authored in HubSpot.
- **Snippets** (`sales_snippets.py`) are short reusable text blocks called
  by `#shortcut` in HubSpot.  Useful when the user asks "what's our
  standard FAQ on management fees" - look it up via `list_sales_snippets`.

## Documents

`list_sales_documents` returns the document library.  Documents have view
tracking via `get_document_view_summary` (mock-only in v0).

## Meeting links

`list_meeting_links` returns the customer's HubSpot scheduling pages.  Pass
the link URL to a contact for them to self-schedule a tour or call.
Booking happens on HubSpot's hosted page; no API surface here.
