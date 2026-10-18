# Linux native SVN listener

The `serve` Linux development component builds a foreground `pgvnserve` using
the pinned Subversion 1.14.5 server protocol, patched repository core, PgVN
storage and authorization code, APR sockets, libpq and POSIX worker threads.
It serves native `svn://` clients; it is not an adapter probe.

This component is not a complete Linux product. It does not build the
administration CLI, Apache/DAV modules, monitor host, installer or production
package. Windows service behavior remains Windows-only. Linux does not support
`--service`, `--service-name`, `--monitor-url`, daemonization or `sd_notify`.
The supported entry points are `pgvnserve --version` and
`pgvnserve --config FILE --monitor-disabled`.

Build with the verified SDK:

```sh
python3 scripts/pgvn/build.py --target linux --components serve \
  --configuration Release --linux-sdk build/pgvn/linux-sdk
python3 scripts/pgvn/build.py --target linux --components serve \
  --configuration Debug --linux-sdk build/pgvn/linux-sdk --sanitize
```

The executable and its pinned runtime libraries are under
`build/pgvn/linux/release-serve/` or
`build/pgvn/linux/debug-serve-sanitized/`. `build-manifest.json` records the
runtime file hashes, ELF dependencies, Subversion archive inputs, prepared
repository/protocol hashes, patch hashes and generated PgFS callback inventory.
The `bin/serve_fixture` executable is test-only.

Create a UTF-8 configuration file readable by the service account:

```ini
[pgvn]
connection = host=127.0.0.1 port=5432 dbname=pgvn user=pgvn_service connect_timeout=3
credential-key-file = /var/lib/pgvn/credential.key
identity-domain = pgvn
listen-host = 127.0.0.1
listen-port = 3690
max-connections = 64
max-request-bytes = 67108864
max-response-bytes = 67108864
read-only = false
```

The database role must have the privileges required by the PgVN catalog and
repository APIs. The Linux `pgvnadmin` executable is not currently built;
create the credential key and store credentials through the public core
credential API. Do not place a key or database password in source control.
The configuration path and file must be valid UTF-8. Startup checks the
catalog and credential key before binding. A readiness line is emitted only
after the listener is bound. Connection strings and credentials are not
included in service diagnostics.

The service remains in the foreground. SIGINT and SIGTERM stop acceptance,
allow up to ten seconds for active requests, then cancel socket I/O and join
workers after their cleanup. Workers are joinable; the service does not
forcibly terminate threads. Database-unavailable startup is retried with
interruptible 1, 2, 4, 8 and 15 second delays. Invalid schema, configuration,
key or bind settings fail with their classified exit code.

Run the isolated integration suite in the pinned Ubuntu 24.04 builder with an
isolated PostgreSQL 18.6 image:

```sh
python3 tests/pgvn/run_linux_serve.py --sdk build/pgvn/linux-sdk \
  --configuration Release
python3 tests/pgvn/run_linux_serve.py --sdk build/pgvn/linux-sdk \
  --configuration Debug --sanitize
```

The runner creates a disposable PostgreSQL container with no published ports,
applies checked migrations as a non-superuser, freezes the product and source
inputs, then exercises a standard SVN client. Evidence is written below
`artifacts/pgvn/linux-native-serve/`. The current suite verifies authenticated
info/checkout/commit/update/cat, wrong-password denial, startup validation,
bind collision, capacity/slot reuse, concurrent handshake/disconnect stress,
stalled-client drain, signal shutdown and port reuse. It also holds the PgFS
transaction table lock while a real SVN commit is in flight, waits for the
PgVN SQL session to block on that lock, signals the service, then releases the
lock and checks that the commit completes within the ten-second drain grace
and remains readable. Startup validation includes an empty-schema database.
Database retry coverage stops startup while the isolated database is
unavailable, verifies that retry waiting is interruptible, and pauses then
resumes PostgreSQL to exercise recovery to readiness. These checks do not
replace broader Linux product acceptance or test a deployed database. The
serve entry point currently initializes telemetry as disabled;
telemetry-enabled serving was not validated.
