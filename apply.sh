#!/usr/bin/env bash
# Apply the Tender & RFP Cockpit schema in order.
#   ./apply.sh DBNAME            upgrade an existing database: runs 002, 003, 004, 005, 006 (all re-runnable)
#   ./apply.sh DBNAME --fresh    drop and recreate DBNAME first, then run everything (DESTROYS its data)
# Uses psql from PATH; set PGHOST/PGUSER/etc. as usual. UPTO=002 / 003 / 004 / 005 stops after that migration.
set -euo pipefail
DB="${1:?usage: apply.sh DBNAME [--fresh]}"
UPTO="${UPTO:-999}"
cd "$(dirname "$0")"
run() { echo "applying $1"; PGOPTIONS='-c client_min_messages=warning' psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -f "$1"; }
if [[ "${2:-}" == "--fresh" ]]; then
  psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" \
                                            -c "CREATE DATABASE \"$DB\""
  # the base files are not re-runnable, so only run them on a fresh database
  for f in schema.sql dedupe.sql review.sql; do run "$f"; done
fi
for f in 002_ingestion.sql 003_source_refs.sql 004_change_tracking.sql 005_platforms_lots.sql 006_seed_platforms.sql; do
  if (( 10#${f%%_*} <= 10#$UPTO )); then run "$f"; fi
done
echo "done: $DB"
