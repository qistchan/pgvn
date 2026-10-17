#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
ini="$PGVN_ETC/monitor.ini"
[[ -f "$ini" && ! -L "$ini" ]] || { echo "missing monitor.ini" >&2; exit 1; }
bind="$(awk -F= '$1=="bind"{print $2}' "$ini")"
port="$(awk -F= '$1=="port"{print $2}' "$ini")"
[[ "$bind" == "127.0.0.1" ]] || { echo "monitor must bind 127.0.0.1" >&2; exit 1; }
[[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] || { echo "invalid monitor port" >&2; exit 1; }
[[ -f "$PGVN_LIB/cache-epoch" ]] || { echo "missing cache epoch" >&2; exit 1; }
if [[ -f "$PGVN_LIB/secrets/credentials.key" ]]; then
    mode="$(stat -c %a "$PGVN_LIB/secrets/credentials.key")"
    [[ "$mode" == "600" ]] || { echo "credential key must be mode 600" >&2; exit 1; }
fi
if [[ -f "$PGVN_ETC/hook-map" ]]; then
    head -n1 "$PGVN_ETC/hook-map" | grep -qx 'PGVNHOOKMAP1' || { echo "invalid hook map" >&2; exit 1; }
fi
echo "configuration ok"
