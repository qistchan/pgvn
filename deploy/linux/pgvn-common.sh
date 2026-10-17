#!/bin/bash
# Shared paths for the Linux deployment scripts. Tests override every root.
set -euo pipefail
PGVN_VERSION="${PGVN_VERSION:-0.1.0-dev}"
PGVN_PREFIX="${PGVN_PREFIX:-/opt/pgvn/${PGVN_VERSION}}"
PGVN_ETC="${PGVN_ETC:-/etc/pgvn}"
PGVN_LIB="${PGVN_LIB:-/var/lib/pgvn}"
PGVN_LOG="${PGVN_LOG:-/var/log/pgvn}"
PGVN_RUN="${PGVN_RUN:-/run/pgvn}"

pgvn_require_dir() {
    local path="$1" mode="$2"
    mkdir -p "$path"
    chmod "$mode" "$path"
    if [[ -L "$path" ]]; then
        echo "refusing symlink: $path" >&2
        exit 1
    fi
}
