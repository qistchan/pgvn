#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export PGVN_PREFIX="$work/opt/pgvn/0.1.0-dev"
export PGVN_ETC="$work/etc"
export PGVN_LIB="$work/lib"
export PGVN_LOG="$work/log"
export PGVN_RUN="$work/run"
export PGVN_SKIP_USER=1
export PGVN_VERSION=0.1.0-dev
"$root/deploy/linux/pgvn-init.sh" >/dev/null
marker="$PGVN_ETC/monitor.ini"
original="$(cat "$marker")"
"$root/deploy/linux/pgvn-init.sh" >/dev/null
[[ "$(cat "$marker")" == "$original" ]]
"$root/deploy/linux/pgvn-check.sh" >/dev/null
printf 'not a map\n' >"$PGVN_ETC/hook-map"
if "$root/deploy/linux/pgvn-check.sh" >/dev/null 2>&1; then echo "bad hook map accepted"; exit 1; fi
rm "$PGVN_ETC/hook-map"
install -d "$PGVN_LIB/secrets"
printf 'secret\n' >"$PGVN_LIB/secrets/credentials.key"
chmod 0644 "$PGVN_LIB/secrets/credentials.key"
if "$root/deploy/linux/pgvn-check.sh" >/dev/null 2>&1; then echo "loose key accepted"; exit 1; fi
chmod 0600 "$PGVN_LIB/secrets/credentials.key"
"$root/deploy/linux/pgvn-check.sh" >/dev/null
dump="$work/pg_dump"
cat >"$dump" <<'EOF'
#!/bin/bash
out=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) out="$2"; shift 2 ;;
        --file=*) out="${1#--file=}"; shift ;;
        *) shift ;;
    esac
done
printf 'fake-dump\n' >"$out"
EOF
chmod +x "$dump"
export PGVN_PG_DUMP="$dump" PGVN_DATABASE_URL="postgresql://example/pgvn"
"$root/deploy/linux/pgvn-backup.sh" "$work/backup" >/dev/null
old="$(cat "$PGVN_LIB/cache-epoch")"
[[ "$(cat "$work/backup/cache-epoch")" == "$old" ]]
[[ "$(cat "$work/backup/database.dump")" == "fake-dump" ]]
rm -rf "$PGVN_ETC" "$PGVN_LIB"
mkdir -p "$PGVN_RUN"
"$root/deploy/linux/pgvn-restore.sh" "$work/backup" >/dev/null
new="$(tr -d '[:space:]' <"$PGVN_LIB/cache-epoch")"
[[ "$new" != "$old" && -n "$new" ]]
[[ -f "$PGVN_ETC/monitor.ini" ]]
if "$root/deploy/linux/pgvn-service.sh" start >/dev/null; then :; fi
rm "$PGVN_LIB/cache-epoch"
if "$root/deploy/linux/pgvn-service.sh" start >/dev/null 2>&1; then echo "start without epoch"; exit 1; fi
printf '%s\n' "$new" >"$PGVN_LIB/cache-epoch"
install -d "$PGVN_PREFIX/bin"
cat >"$PGVN_PREFIX/bin/pgvn-monitor" <<'EOF'
#!/bin/bash
printf '%s\n' "$@"
EOF
chmod +x "$PGVN_PREFIX/bin/pgvn-monitor"
recorded="$("$root/deploy/linux/pgvn-monitor-run.sh")"
grep -q -- '--bind' <<<"$recorded"
grep -q '127.0.0.1' <<<"$recorded"
grep -q 'User=pgvn' "$root/deploy/linux/pgvn.service"
grep -q 'KillSignal=SIGTERM' "$root/deploy/linux/pgvn.service"
grep -q 'TimeoutStopSec=20' "$root/deploy/linux/pgvn.service"
"$root/deploy/linux/pgvn-apache-check.sh" | grep -q 'apache_module=not_built'
echo "deploy checks passed"
