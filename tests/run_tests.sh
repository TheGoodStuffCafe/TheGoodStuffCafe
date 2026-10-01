#!/bin/bash
# Fresh database every run. Needs a Postgres you can create databases on.
#   PGHOST=/tmp PGPORT=5433 PGUSER=postgres ./tests/run_tests.sh
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-5433}"; PGUSER="${PGUSER:-postgres}"
DB="gs_$(date +%s)_$RANDOM"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -qc "CREATE DATABASE $DB"
export TEST_DATABASE_URL="postgresql://$PGUSER@/$DB?host=$PGHOST&port=$PGPORT"
cd "$HERE/.." && python3 -m pytest -q tests/test_app.py "$@"
