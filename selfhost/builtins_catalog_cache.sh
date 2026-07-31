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
BUILTINS_CATALOG_KEEP="${BUILTINS_CATALOG_KEEP:-3}"

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

  {
    printf 'BEGIN;\n'
    # Data-only restore into a freshly migrated database: suppress FK triggers
    # so table order does not matter, and keep sequences untouched.
    printf 'SET session_replication_role = replica;\n'
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
    # Bootstrap bookkeeping carries the desired_hash that lets the existing
    # guard short-circuit the sync; it is not project-scoped.
    local bookkeeping
    for bookkeeping in integration_bootstrap_state integration_backends; do
      builtins_catalog_psql -tAc "SELECT to_regclass('public.${bookkeeping}')" \
        2>/dev/null | grep -q . || continue
      printf 'COPY public.%s FROM stdin;\n' "$bookkeeping"
      builtins_catalog_psql -tAc "COPY (SELECT * FROM ${bookkeeping}) TO STDOUT"
      printf '\\.\n'
    done
    printf 'COMMIT;\n'
  } 2>/dev/null | gzip >"$tmp" || { rm -f "$tmp"; return 1; }

  mv -f "$tmp" "$target"
  builtins_catalog_prune
  return 0
}

# Load a snapshot matching the current schema and manifest. No match, an
# existing catalogue, or any load failure leaves the database untouched and
# lets the normal sync run.
builtins_catalog_restore() {
  local manifest="${1:-}"
  builtins_catalog_db_ready || return 1
  [[ -z "$(builtins_catalog_project_id)" ]] || return 1

  local key snapshot
  key="$(builtins_catalog_cache_key "$manifest")" || return 1
  snapshot="$(builtins_catalog_cache_dir)/$key.sql.gz"
  [[ -f "$snapshot" ]] || return 1

  if ! gunzip -c "$snapshot" | builtins_catalog_psql_stdin -q -v ON_ERROR_STOP=1 >/dev/null 2>&1; then
    # The transaction rolled back; drop the snapshot so it cannot fail again.
    rm -f "$snapshot"
    return 1
  fi

  # A snapshot carrying the bootstrap hash but no catalogue would let the seed
  # short-circuit against an empty catalogue, so require both.
  local project_id restored
  project_id="$(builtins_catalog_project_id)"
  [[ -n "$project_id" ]] || return 1
  restored="$(builtins_catalog_psql -tAc \
    "SELECT count(*) FROM log_event WHERE project_id = ${project_id}" 2>/dev/null | tr -d '[:space:]')"
  if [[ -z "$restored" || "$restored" == "0" ]]; then
    builtins_catalog_discard "$project_id"
    rm -f "$snapshot"
    return 1
  fi
  return 0
}

# Roll back a partial restore so the normal sync starts from a clean database.
builtins_catalog_discard() {
  local project_id="$1"
  local table
  {
    printf 'BEGIN;\n'
    printf 'SET session_replication_role = replica;\n'
    while read -r table; do
      [[ -n "$table" ]] || continue
      printf 'DELETE FROM public.%s WHERE project_id = %s;\n' "$table" "$project_id"
    done < <(builtins_catalog_tables)
    printf 'DELETE FROM public.project WHERE id = %s;\n' "$project_id"
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
