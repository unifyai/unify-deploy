# HubSpot Service Hub

Conversations Inbox + Knowledge Base + Chatflows.

## Conversations

A "conversation" is a thread in HubSpot's shared inbox.  It collects
messages from email, live chat, Facebook Messenger, etc.

- `list_conversation_threads(status=...)` finds open / closed threads.
- `list_conversation_messages(thread_id)` reads the back-and-forth.
- `send_conversation_reply(thread_id, text, confirm=True)` posts a reply.
  HIGH-STAKES: customer sees the reply.  Confirm with the user before passing
  `confirm=True` - check the recipient and the message tone.

Tier-gated: requires Service Hub.  Free portals can read but not reply.

## Knowledge Base

- `list_kb_articles` / `get_kb_article` to browse the help center.
- `create_kb_article(title, content_html, slug, ...)` creates a draft.
- `update_kb_article(article_id, properties)` edits.

KB articles publish via `update_kb_article(..., {"currentState": "PUBLISHED"})`.

When the user asks "do we have a help article on X", first
`list_kb_articles` (or `query_local` once articles are synced), then
present the relevant ones with their URLs.

## Chatflows

Read-only definitions of chatbot/live-chat flows.  Useful for the user to
review what flows exist; not editable via this package.
