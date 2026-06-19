# Web Content Fetching

Use the fetch tool to retrieve web page content as clean markdown.

## When to use

- The user asks about something on a specific URL
- You need current information from a web page
- You want to read documentation or articles

## How to call

The MCP fetch server exposes a single `fetch` tool:

```
fetch(url="https://example.com")
```

The server downloads the page and converts its HTML to markdown,
stripping navigation, ads, and boilerplate so the result is compact
enough for LLM context.

## Tips

- Always pass the full URL including the scheme (`https://`).
- For large pages the server may truncate the response; request a
  more specific URL (e.g. a docs subpage) when possible.
- The server respects `robots.txt` by default.
- Rate-limit yourself: avoid fetching the same URL repeatedly within
  a single conversation.
