#!/usr/bin/env bash
# Query the local BokaClaw Postgres cluster without a host psql install.
# Runs psql inside the bokaclaw-postgres container via docker exec.
#
# Usage: db.sh [flags] <command> [args]
# Run `db.sh --help` for the full reference.
set -euo pipefail

CONTAINER=${PG_CONTAINER:-bokaclaw-postgres}
PGUSER=${PG_USER:-postgres}
DB=${PG_DB:-mem0_app}
FORMAT=table
WRITE=0

die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Query the local BokaClaw Postgres cluster (psql runs inside the container).

Usage:
  db.sh [flags] <command> [args]

Flags (must come before the command):
  -d, --db <name>    Database: mem0_app (default) or postgres
      --csv          CSV output for `query` (no headers alignment)
  -w, --write        Allow writes. Default is a read-only transaction.
  -h, --help         Show this help.

Commands:
  tables                       Tables with exact row counts and on-disk size
  databases                    Databases with sizes
  schema <table>               Columns, indexes and constraints (\d+)
  count <table>                Exact row count
  query <sql>                  Run SQL (or read SQL from stdin if omitted)
  help                         This help

Examples:
  db.sh tables
  db.sh -d postgres tables                 # legacy Mem0 tables
  db.sh schema conversations
  db.sh count request_logs
  db.sh query "SELECT id, title FROM conversations ORDER BY created_at DESC LIMIT 5"
  db.sh query < report.sql
  db.sh --csv query "SELECT * FROM request_logs LIMIT 20" > logs.csv
  db.sh -w query "UPDATE conversations SET title = 'x' WHERE id = '...'"
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--db)
      DB=${2:-}
      [ -n "$DB" ] || die "--db needs a value"
      shift 2
      ;;
    --csv)
      FORMAT=csv
      shift
      ;;
    -w|--write)
      WRITE=1
      shift
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "unknown flag: $1 (flags must come before the command)"
      ;;
    *)
      break
      ;;
  esac
done

CMD=${1:-tables}
[ $# -gt 0 ] && shift

[[ "$DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid database name: $DB"
[[ "$DB" = "mem0_app" || "$DB" = "postgres" ]] || \
  echo "warning: $DB is not one of the usual databases (mem0_app, postgres)" >&2

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  die "container '$CONTAINER' is not running; start the stack with .claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh"
fi

run_psql() {
  local -a opts=(-X -v ON_ERROR_STOP=1 -P pager=off)
  [ "$FORMAT" = csv ] && opts+=(--csv)
  local -a cmd=(docker exec -i)
  if [ "$WRITE" = 0 ]; then
    cmd+=(-e "PGOPTIONS=-c default_transaction_read_only=on")
  fi
  cmd+=("$CONTAINER" psql -U "$PGUSER" -d "$DB" "${opts[@]}")
  "${cmd[@]}" "$@"
}

require_table() {
  local table=${1:-}
  [ -n "$table" ] || die "command '$CMD' needs a table name"
  [[ "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$ ]] || \
    die "invalid table name: $table"
  printf '%s' "$table"
}

case "$CMD" in
  tables)
    run_psql -c "
      SELECT n.nspname AS schema,
             c.relname  AS table,
             (xpath('/row/cnt/text()',
                    query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname),
                                 false, true, '')))[1]::text::bigint AS rows,
             pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE c.relkind IN ('r', 'p')
        AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      ORDER BY pg_total_relation_size(c.oid) DESC, n.nspname, c.relname;"
    ;;

  databases)
    run_psql -c "
      SELECT datname AS database,
             pg_size_pretty(pg_database_size(datname)) AS size
      FROM pg_database
      WHERE datistemplate = false
      ORDER BY pg_database_size(datname) DESC;"
    ;;

  schema)
    table=$(require_table "${1:-}")
    run_psql -c "\\d+ $table"
    ;;

  count)
    table=$(require_table "${1:-}")
    if [[ "$table" == *.* ]]; then
      qualified="\"${table%%.*}\".\"${table#*.}\""
    else
      qualified="\"$table\""
    fi
    run_psql -c "SELECT count(*) AS rows FROM $qualified;"
    ;;

  query)
    if [ $# -eq 0 ]; then
      [ -t 0 ] && die "query needs SQL as an argument or on stdin"
      SQL=$(cat)
    else
      SQL="$*"
    fi
    [ -n "${SQL//[[:space:]]/}" ] || die "empty SQL"
    if [ "$WRITE" = 0 ]; then
      # The transaction is read-only regardless; this is a clearer error for
      # the common accidental-write case.
      if ! printf '%s' "$SQL" | sed -E 's/^[[:space:]]+//' | grep -Eiq \
        '^(\(*[[:space:]]*)?(select|with|show|table|values|explain|\\d|\\l|\\x|\\timing)'; then
        die "read-only mode: only SELECT/WITH/SHOW/TABLE/VALUES/EXPLAIN and \\d commands are allowed; re-run with --write to modify data"
      fi
    fi
    run_psql -c "$SQL"
    ;;

  help)
    usage
    ;;
  *)
    die "unknown command: $CMD (try: tables, databases, schema, count, query)"
    ;;
esac
