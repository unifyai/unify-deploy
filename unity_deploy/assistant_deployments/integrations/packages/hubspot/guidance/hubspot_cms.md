# HubSpot CMS Hub

Pages, blog posts, files, HubDB tables, URL redirects, domains.

## Pages and blog posts

Both `cms_pages.py` and `cms_blog_posts.py` follow the same pattern:

1. **Create** in `DRAFT` state via `create_*`.
2. **Edit** via `update_*`.
3. **Publish** via `publish_*` - HIGH-STAKES, requires `confirm=True` AND
   `HUBSPOT_ALLOW_CMS_PUBLISH=true`.

Always read content back to the user after a non-trivial edit before
publishing.  Pages and posts are public-facing.

## Files

- `list_cms_files`, `get_cms_file` to browse.
- `upload_cms_file(file_path, folder_path)` uploads a local file to the
  HubSpot file library.  Returns the file's HubSpot URL.
- `delete_cms_file` - gated by `HUBSPOT_ALLOW_DELETE`.

## HubDB

HubDB is HubSpot's structured table store - useful for property catalogs,
team rosters, locations, etc. that get rendered into CMS pages.

- `list_hubdb_tables` / `get_hubdb_table` for schema.
- `list_hubdb_rows`, `create_hubdb_row`, `update_hubdb_row`,
  `delete_hubdb_row` for content.
- `publish_hubdb_table(table_id, confirm=True)` to push the draft live -
  HIGH-STAKES.

HubDB row edits go to a draft state until the table is published.

## URL Redirects

Standard CRUD; useful when migrating page slugs.

## Domains

Read-only listing.  Domain configuration happens in HubSpot's UI.
