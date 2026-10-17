#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
output="${1:-}"
[[ -n "$output" ]] || { echo "usage: pgvn-backup.sh OUTPUT_DIR" >&2; exit 1; }
mkdir -p "$output/etc" "$output/secrets"
dump="${PGVN_PG_DUMP:-pg_dump}"
database="${PGVN_DATABASE_URL:-}"
[[ -n "$database" ]] || { echo "PGVN_DATABASE_URL is required" >&2; exit 1; }
"$dump" --file="$output/database.dump" --dbname="$database"
if [[ -d "$PGVN_ETC" ]]; then cp -a "$PGVN_ETC/." "$output/etc/"; fi
if [[ -d "$PGVN_LIB/secrets" ]]; then cp -a "$PGVN_LIB/secrets/." "$output/secrets/"; fi
epoch="$(cat "$PGVN_LIB/cache-epoch")"
printf '%s\n' "$epoch" >"$output/cache-epoch"
chmod 0700 "$output"
echo "backup $epoch"
