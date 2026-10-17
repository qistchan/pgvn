#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
backup="${1:-}"
[[ -d "$backup" && -f "$backup/cache-epoch" && -f "$backup/database.dump" ]] || {
    echo "usage: pgvn-restore.sh BACKUP_DIR" >&2; exit 1; }
if [[ -f "$PGVN_RUN/pgvn-monitor.pid" ]]; then
    pid="$(cat "$PGVN_RUN/pgvn-monitor.pid")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "stop PgVN before restore" >&2
        exit 1
    fi
fi
old="$(tr -d '[:space:]' <"$backup/cache-epoch")"
new="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
[[ -n "$old" && "$old" != "$new" ]] || { echo "cache epoch was not rotated" >&2; exit 1; }
mkdir -p "$PGVN_ETC" "$PGVN_LIB/secrets"
if [[ -d "$backup/etc" ]]; then cp -a "$backup/etc/." "$PGVN_ETC/"; fi
if [[ -d "$backup/secrets" ]]; then cp -a "$backup/secrets/." "$PGVN_LIB/secrets/"; fi
cp -a "$backup/database.dump" "$PGVN_LIB/database.dump"
printf '%s\n' "$new" >"$PGVN_LIB/cache-epoch"
chmod 0640 "$PGVN_LIB/cache-epoch"
echo "restored epoch $new"
