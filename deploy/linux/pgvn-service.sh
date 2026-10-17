#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/pgvn-common.sh"
command="${1:-diagnose}"
case "$command" in
    diagnose)
        "$(dirname "$0")/pgvn-check.sh"
        if [[ -f "$PGVN_LIB/cache-epoch" ]]; then echo "cache_epoch=$(cat "$PGVN_LIB/cache-epoch")"; fi
        ;;
    install)
        [[ "$(id -u)" -eq 0 ]] || { echo "install requires root" >&2; exit 1; }
        install -d /etc/systemd/system
        sed "s#@PREFIX@#${PGVN_PREFIX}#g; s#@ETC@#${PGVN_ETC}#g" "$(dirname "$0")/pgvn.service" >/etc/systemd/system/pgvn.service
        echo "installed pgvn.service"
        ;;
    start)
        "$(dirname "$0")/pgvn-check.sh" >/dev/null
        [[ -s "$PGVN_LIB/cache-epoch" ]] || { echo "refusing to start without a cache epoch" >&2; exit 1; }
        echo "start requires systemd on the target host; unit is ready"
        ;;
    stop)
        echo "stop sends SIGTERM and drains for TimeoutStopSec"
        ;;
    *)
        echo "usage: pgvn-service.sh diagnose|install|start|stop" >&2
        exit 2
        ;;
esac
