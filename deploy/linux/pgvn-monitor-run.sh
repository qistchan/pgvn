#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
ini="$PGVN_ETC/monitor.ini"
binary="${PGVN_MONITOR_BIN:-$PGVN_PREFIX/bin/pgvn-monitor}"
[[ -x "$binary" && -f "$ini" ]] || { echo "monitor binary or ini missing" >&2; exit 1; }
declare -A values=()
while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    values["$key"]="$value"
done <"$ini"
exec "$binary" \
    --bind "${values[bind]}" \
    --port "${values[port]}" \
    --web "${values[web]}" \
    --tokens "${values[tokens]}" \
    --timeout "${values[timeout]}" \
    --queue "${values[queue]}" \
    --drain-ms "${values[drain_ms]}"
