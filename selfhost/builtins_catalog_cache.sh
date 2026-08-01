#!/usr/bin/env bash
# Snapshot/restore the Builtins catalogue across a fresh redeploy.
#
# The catalogue is upstream reference data — ~40k rows synced from the provider
# for a given manifest, identical on every install, and ~30 minutes to rebuild.
# Orchestra already guards the sync with `integration_bootstrap_state`'s
# desired_hash, but `up` deletes the Postgres volume, which takes the catalogue
# *and* the hash that guards it. The guard can therefore never fire on `up`, so
# every redeploy re-syncs the whole catalogue from the network.
#
# Keeping a snapshot outside the database restores that guard's effectiveness:
# the state comes back, the existing hash comparison runs, and an unchanged
# manifest short-circuits the sync. Only the Builtins project is captured, so
# developer data is still genuinely purged.
#
# The cache key pins the two inputs that make a snapshot loadable and correct:
# the schema (alembic head) and the manifest (content hash). A miss on either
# falls through to the normal sync, so a stale snapshot is never applied.

BUILTINS_CATALOG_PROJECT="${BUILTINS_CATALOG_PROJECT:-Builtins}"
BUILTINS_CATALOG_STATE_TABLE="integration_bootstrap_state"
BUILTINS_CATALOG_KEEP="${BUILTINS_CATALOG_KEEP:-3}"

# Optional https base URL holding published snapshots as <base>/<key>.sql.gz.
# The catalogue is provider metadata that is identical for a given schema and
# manifest, so one machine's snapshot seeds any other — a first `up` on a new
# machine becomes a download instead of a full provider sync.
#
# A snapshot is SQL executed against the local database, so this must only ever
# point at a location the operator controls. Downloads are structurally
# validated before use (see builtins_catalog_validate), which bounds a corrupt
# or truncated file but is not a substitute for trusting the host.
BUILTINS_CATALOG_URL="${UNITY_BUILTINS_CATALOG_URL:-}"
BUILTINS_CATALOG_FETCH_TIMEOUT="${UNITY_BUILTINS_CATALOG_FETCH_TIMEOUT:-600}"

builtins_catalog_cache_dir() {
  printf '%s' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/builtins-catalog"
}

builtins_catalog_db_container() {
  printf '%s' "${ORCHESTRA_DB_CONTAINER:-orchestra-local-db}"
}

# Run SQL in the local database. Extra args go to psql.
#
# Deliberately not `docker exec -i`: that attaches the caller's stdin, which a
# loop reading a table list would then lose to the first iteration — silently
# dumping one table instead of all of them.
builtins_catalog_psql() {
  docker exec "$(builtins_catalog_db_container)" \
    psql -U "${ORCHESTRA_DB_USER:-orchestra}" \
    -d "${ORCHESTRA_DB_BASE:-orchestra}" \
    "$@" </dev/null
}

# Feed SQL to the local database on stdin.
builtins_catalog_psql_stdin() {
  docker exec -i "$(builtins_catalog_db_container)" \
    psql -U "${ORCHESTRA_DB_USER:-orchestra}" \
    -d "${ORCHESTRA_DB_BASE:-orchestra}" \
    "$@"
}

builtins_catalog_db_ready() {
  command -v docker &>/dev/null || return 1
  builtins_catalog_psql -tAc 'SELECT 1' &>/dev/null
}

# <alembic-head>-<manifest-sha12>. A schema or manifest change yields a new key,
# so an unloadable or out-of-date snapshot simply misses.
builtins_catalog_cache_key() {
  local manifest="${1:-}"
  local head manifest_sha
  head="$(builtins_catalog_psql -tAc \
    'SELECT version_num FROM alembic_version LIMIT 1' 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$head" ]] || return 1
  if [[ -n "$manifest" && -f "$manifest" ]]; then
    manifest_sha="$(shasum -a 256 "$manifest" | cut -c1-12)"
  else
    manifest_sha="nomanifest"
  fi
  printf '%s-%s' "$head" "$manifest_sha"
}

builtins_catalog_project_id() {
  builtins_catalog_psql -tAc \
    "SELECT id FROM project WHERE name = '${BUILTINS_CATALOG_PROJECT}' LIMIT 1" \
    2>/dev/null | tr -d '[:space:]'
}

# Project-scoped tables, parents only — COPY routes partitioned writes itself.
builtins_catalog_tables() {
  builtins_catalog_psql -tAc "
    SELECT c.relname
    FROM information_schema.columns col
    JOIN pg_class c ON c.relname = col.table_name
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    WHERE col.column_name = 'project_id'
      AND col.table_schema = 'public'
      AND c.relkind IN ('r', 'p')
      AND NOT c.relispartition
    GROUP BY c.relname
    ORDER BY c.relname
  " 2>/dev/null | tr -d '\r'
}

# Write a restorable snapshot of the Builtins project. Emitted as one plain-SQL
# transaction so a restore either lands whole or not at all.
builtins_catalog_save() {
  local manifest="${1:-}"
  builtins_catalog_db_ready || return 1

  local project_id
  project_id="$(builtins_catalog_project_id)"
  [[ -n "$project_id" ]] || return 1

  local key cache_dir target tmp
  key="$(builtins_catalog_cache_key "$manifest")" || return 1
  cache_dir="$(builtins_catalog_cache_dir)"
  mkdir -p "$cache_dir"
  target="$cache_dir/$key.sql.gz"
  tmp="$target.$$.partial"

  # Data only. Transaction control and clearing the placeholder project belong
  # to the restore, which is the side that knows what it is loading into.
  {
    printf 'COPY public.project FROM stdin;\n'
    builtins_catalog_psql -tAc \
      "COPY (SELECT * FROM project WHERE id = ${project_id}) TO STDOUT"
    printf '\\.\n'
    local table
    while read -r table; do
      [[ -n "$table" ]] || continue
      [[ "$table" == "project" ]] && continue
      printf 'COPY public.%s FROM stdin;\n' "$table"
      builtins_catalog_psql -tAc \
        "COPY (SELECT * FROM ${table} WHERE project_id = ${project_id}) TO STDOUT"
      printf '\\.\n'
    done < <(builtins_catalog_tables)
    # Carries the desired_hash that lets Orchestra's guard short-circuit the
    # sync, and is not project-scoped so it needs capturing by name.
    #
    # integration_backends is deliberately excluded: Orchestra's self-host
    # bootstrap recreates those rows on every start, so shipping them in a
    # snapshot only collides with the ones already there.
    if builtins_catalog_psql -tAc \
      "SELECT to_regclass('public.${BUILTINS_CATALOG_STATE_TABLE}')" 2>/dev/null | grep -q .; then
      printf 'COPY public.%s FROM stdin;\n' "$BUILTINS_CATALOG_STATE_TABLE"
      builtins_catalog_psql -tAc \
        "COPY (SELECT * FROM ${BUILTINS_CATALOG_STATE_TABLE}) TO STDOUT"
      printf '\\.\n'
    fi
  } 2>/dev/null | gzip >"$tmp" || { rm -f "$tmp"; return 1; }

  mv -f "$tmp" "$target"
  builtins_catalog_prune
  return 0
}

# A snapshot must be nothing but `COPY public.<table> FROM stdin;` blocks and
# their data. Data lines are consumed by COPY and cannot execute, so refusing
# every other statement keeps a downloaded file from running arbitrary SQL as
# the database superuser.
builtins_catalog_validate() {
  gunzip -c "$1" 2>/dev/null | awk '
    BEGIN { in_copy = 0 }
    in_copy { if ($0 == "\\.") in_copy = 0; next }
    /^COPY public\.[a-z_]+ FROM stdin;$/ { in_copy = 1; blocks++; next }
    /^$/ { next }
    { print "unexpected statement: " substr($0, 1, 60) > "/dev/stderr"; bad = 1; exit }
    END { if (bad || in_copy || blocks == 0) exit 1 }
  ' 2>/dev/null
}

# Fetch a published snapshot for this key when the local cache has none.
builtins_catalog_fetch() {
  local key="$1" target="$2"
  [[ -n "$BUILTINS_CATALOG_URL" ]] || return 1
  [[ "$BUILTINS_CATALOG_URL" == https://* ]] || return 1
  command -v curl &>/dev/null || return 1

  local tmp="${target}.download.$$"
  mkdir -p "$(dirname "$target")"
  if ! curl -fsSL --max-time "$BUILTINS_CATALOG_FETCH_TIMEOUT" \
    -o "$tmp" "${BUILTINS_CATALOG_URL%/}/${key}.sql.gz"; then
    rm -f "$tmp"
    return 1
  fi
  if ! gzip -t "$tmp" 2>/dev/null || ! builtins_catalog_validate "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$target"
  return 0
}

# Load a snapshot matching the current schema and manifest. No match, an
# already-populated catalogue, or any load failure leaves the database usable
# and lets the normal sync run.
builtins_catalog_restore() {
  local manifest="${1:-}"
  builtins_catalog_db_ready || return 1

  # Orchestra's self-host bootstrap creates an empty Builtins project (and its
  # contexts) as it starts, so the project existing means nothing. Only a
  # populated catalogue is a reason not to load one.
  local existing_id
  existing_id="$(builtins_catalog_project_id)"
  if [[ -n "$existing_id" ]]; then
    [[ "$(builtins_catalog_row_count "$existing_id")" == "0" ]] || return 1
  fi

  local key snapshot
  key="$(builtins_catalog_cache_key "$manifest")" || return 1
  snapshot="$(builtins_catalog_cache_dir)/$key.sql.gz"
  if [[ ! -f "$snapshot" ]]; then
    builtins_catalog_fetch "$key" "$snapshot" || return 1
  fi

  # Clear the placeholder and load in one transaction: the snapshot carries the
  # project row with its original id, which the rows reference.
  if ! {
    printf 'BEGIN;\n'
    # Data-only load into a freshly migrated database: suppress FK triggers so
    # table order does not matter, and leave sequences untouched.
    printf 'SET session_replication_role = replica;\n'
    [[ -n "$existing_id" ]] && builtins_catalog_delete_sql "$existing_id"
    gunzip -c "$snapshot"
    printf 'COMMIT;\n'
  } | builtins_catalog_psql_stdin -q -v ON_ERROR_STOP=1 >/dev/null 2>&1; then
    # The transaction rolled back, so the placeholder is intact; drop the
    # snapshot so a bad one cannot fail every redeploy.
    rm -f "$snapshot"
    return 1
  fi

  # A snapshot carrying the bootstrap hash but no catalogue would let the seed
  # short-circuit against an empty catalogue, so require both.
  local project_id
  project_id="$(builtins_catalog_project_id)"
  [[ -n "$project_id" ]] || return 1
  if [[ "$(builtins_catalog_row_count "$project_id")" == "0" ]]; then
    builtins_catalog_discard "$project_id"
    rm -f "$snapshot"
    return 1
  fi
  return 0
}

# Size of a project's catalogue. `0` for an empty placeholder project.
builtins_catalog_row_count() {
  builtins_catalog_psql -tAc \
    "SELECT count(*) FROM log_event WHERE project_id = ${1}" 2>/dev/null |
    tr -d '[:space:]'
}

# DELETE statements clearing a project's catalogue, project row included, plus
# the bootstrap state the snapshot replaces. Everything the snapshot writes must
# be cleared first, or loading it collides with what is already there.
builtins_catalog_delete_sql() {
  local project_id="$1"
  local table
  while read -r table; do
    [[ -n "$table" ]] || continue
    printf 'DELETE FROM public.%s WHERE project_id = %s;\n' "$table" "$project_id"
  done < <(builtins_catalog_tables)
  printf 'DELETE FROM public.project WHERE id = %s;\n' "$project_id"
  printf 'DELETE FROM public.%s;\n' "$BUILTINS_CATALOG_STATE_TABLE"
}

# Undo a restore that produced an empty catalogue, so the sync starts clean.
builtins_catalog_discard() {
  {
    printf 'BEGIN;\n'
    printf 'SET session_replication_role = replica;\n'
    builtins_catalog_delete_sql "$1"
    printf 'COMMIT;\n'
  } | builtins_catalog_psql_stdin -q -v ON_ERROR_STOP=1 >/dev/null 2>&1 || true
}

builtins_catalog_prune() {
  local cache_dir
  cache_dir="$(builtins_catalog_cache_dir)"
  [[ -d "$cache_dir" ]] || return 0
  local stale
  # shellcheck disable=SC2012 # names here are our own keys, never arbitrary
  stale="$(ls -t "$cache_dir"/*.sql.gz 2>/dev/null | tail -n "+$((BUILTINS_CATALOG_KEEP + 1))")"
  [[ -n "$stale" ]] || return 0
  printf '%s\n' "$stale" | while read -r file; do
    [[ -n "$file" ]] && rm -f "$file"
  done
}
