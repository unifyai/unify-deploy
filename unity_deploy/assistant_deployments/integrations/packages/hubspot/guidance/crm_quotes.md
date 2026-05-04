# HubSpot Quotes

Build, list, and send quotes.

## Build

1. `create_quote({"hs_title": "...", "hs_expiration_date": "YYYY-MM-DD", ...})`.
2. Associate to the deal: `create_association(from_object_type="quotes", from_id=quote_id, to_object_type="deals", to_id=deal_id)`.
3. Add line items: `create_line_item(...)` for each, then associate the
   line items to both the quote and the deal.

## Send (HIGH-STAKES)

`send_quote(quote_id, confirm=True)` transitions the quote to
`PENDING_BUYER_SIGNATURE` and emails it to associated contacts.

- Requires `confirm=True` from the caller.
- Confirm with the user before passing `confirm=True`: confirm the
  recipient list, the line items, and the total amount.
- If something is wrong, walk back: update line items / properties, then
  resend.

## States

`DRAFT` → `APPROVAL_NOT_NEEDED` (or `PENDING_APPROVAL` → `APPROVED`) →
`PENDING_BUYER_SIGNATURE` → `SIGNED` / `EXPIRED` / `REJECTED`.  Quotes can't
be edited once they're past `DRAFT`.
