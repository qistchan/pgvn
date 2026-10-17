#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
pgvn_require_dir "$PGVN_PREFIX" 0755
pgvn_require_dir "$PGVN_PREFIX/bin" 0755
pgvn_require_dir "$PGVN_PREFIX/share/web" 0755
pgvn_require_dir "$PGVN_ETC" 0750
pgvn_require_dir "$PGVN_LIB" 0750
pgvn_require_dir "$PGVN_LIB/secrets" 0700
pgvn_require_dir "$PGVN_LOG" 0750
if [[ "${PGVN_SKIP_USER:-0}" != 1 && "$(id -u)" -eq 0 ]]; then
    if ! id pgvn >/dev/null 2>&1; then
        useradd --system --home-dir "$PGVN_LIB" --shell /usr/sbin/nologin pgvn
    fi
    chown -R pgvn:pgvn "$PGVN_LIB" "$PGVN_LOG" "$PGVN_ETC"
fi
if [[ ! -f "$PGVN_ETC/monitor.ini" ]]; then
    cat >"$PGVN_ETC/monitor.ini" <<EOF
bind=127.0.0.1
port=14691
web=${PGVN_PREFIX}/share/web
tokens=${PGVN_ETC}/monitor.tokens
timeout=5
queue=64
drain_ms=15000
EOF
    chmod 0640 "$PGVN_ETC/monitor.ini"
fi
if [[ ! -f "$PGVN_LIB/cache-epoch" ]]; then
    epoch="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    printf '%s\n' "$epoch" >"$PGVN_LIB/cache-epoch"
    chmod 0640 "$PGVN_LIB/cache-epoch"
fi
echo "initialized ${PGVN_PREFIX}"
