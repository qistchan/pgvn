#!/bin/bash
set -euo pipefail
example="$(dirname "$0")/apache/pgvn-dav.conf.example"
grep -q 'DAV pgvn' "$example"
grep -q 'Require valid-user' "$example"
grep -q 'AuthType Basic' "$example"
grep -q 'PgVNConfig' "$example"
if [[ -x "${PGVN_MODULE:-}" ]] && command -v apachectl >/dev/null 2>&1; then
    apachectl -t -f "$example"
else
    echo "apache_module=not_built"
fi
