# Linux native administration CLI

The `admin` Linux development component builds a real `pgvnadmin` against the
pinned Subversion 1.14.5 repository layer, PgFS and PgVN core. It also includes
the foreground `pgvnserve` for integration testing. Database initialization,
repositories, versioned policy, credentials, monitor tokens and SVN dump/load
use the production implementations, not a test adapter. SQL migrations and
their checksums are embedded in the executable.

This is not a complete Linux product or a deployment package. Linux
`maintenance freeze` is explicitly rejected before opening a database or
starting a child: safe process-tree containment for arbitrary administration
children is not yet implemented. Monitor HTTP, Apache/DAV, systemd/service
accounts, backup/restore automation and the full protocol matrix remain
separate work. Creating monitor tokens does not enable a Linux monitor server.

## Build and run

Use the verified SDK and the Ubuntu 24.04 builder environment described in
[the native service guide](linux-native-serve.md):

```sh
python3 scripts/pgvn/build.py --target linux --components admin \
  --configuration Release --linux-sdk build/pgvn/linux-sdk
python3 scripts/pgvn/build.py --target linux --components admin \
  --configuration Debug --linux-sdk build/pgvn/linux-sdk --sanitize
```

Outputs are under `build/pgvn/linux/release-admin/` and
`build/pgvn/linux/debug-admin-sanitized/`. Keep each `bin/` directory with its
matching `lib/`; these are development artifacts, not system-installed files.
The build manifest records ELF dependencies, SDK/source/patch provenance,
generated inputs and runtime hashes. Run `bin/pgvnadmin --version` to inspect
the actual build identity and `bin/pgvnadmin --help` for command syntax.

The shared [administration guide](M2-admin.zh-CN.md) describes command
semantics; its original Windows paths and historical schema versions are not
Linux configuration instructions. The current build uses catalog schema 8
and FS schema 4. Linux arguments, environment connection strings, paths and
configuration must be UTF-8.

## Database and credentials

A database administrator must first create the target database and login
roles. Use a non-superuser migration owner with permission to create schemas
in that database. The CLI does not create PostgreSQL databases or login roles.
Read connection settings from `PGVN_DSN` or `--config FILE`; a supplied config
file takes precedence. Keep database passwords out of command-line arguments,
logs and source control. Prefer a protected libpq service/password file.

Example migration configuration:

```ini
[pgvn]
connection = service=pgvn-migration
```

With `pgvnadmin` on PATH and protected configuration files:

```sh
pgvnadmin --config migration.ini db init
pgvnadmin --config migration.ini db migrate
pgvnadmin --config migration.ini db check
pgvnadmin --config migration.ini db grant --admin-role pgvn_admin \
  --service-role pgvn_service --monitor-role pgvn_monitor
pgvnadmin key create /absolute/private/credentials.key key-1
```

`key create` is offline and creates a mode-0600 key, never overwriting an
existing file. Use a directory owned by the effective user or root, not writable
by group/others; symlinks are rejected in every path component. Key reads
require a regular, singly linked file owned by the effective user with mode
0400 or 0600. Creating a key as a different user from the service does not
establish a production key distribution procedure.

An administration configuration can also include:

```ini
[pgvn]
connection = service=pgvn-admin
credential-key-file = /absolute/private/credentials.key
```

Passwords for `user set DOMAIN USER` are read only from redirected stdin.
Interactive terminal input, embedded NUL and oversized input are rejected;
one trailing LF or CRLF is removed. Do not use an actual password literal in
shell history. Credential rekey uses the new key and `--old-key-file` (or the
corresponding config setting); coordinate any service configuration change
separately. The CLI cannot automatically rotate running hosts' key material.

New repositories deny access by default. Publish an authz/settings generation
with the intended identity domain, create credentials in that domain, then
use the native service guide to configure `pgvnserve --monitor-disabled`.
The trusted service role can perform permitted read-only `look` operations;
it is not an administrative role.

## History and cancellation

Disable a destination before `load`. Import revisions are committed one at a
time: a later error or cancellation does not undo already committed revisions.
Use `look` and `verify` before re-enabling it. Binary file contents and standard
SVN dump streams are not converted to text. `dump --file` creates exclusively;
existing files are not overwritten. `dump` rejects `--json`.

SIGINT and SIGTERM request cooperative cancellation through the existing core
cancellation event and SVN callbacks. Idle password and load stdin reads are
poll-based on Linux. Cancellation is not a forced thread termination or a
promise of whole-command rollback. Exit classes and structured `--json`
diagnostics follow the shared administration contract. Broken-pipe output is
treated as a failure rather than terminating on SIGPIPE.

An already-running synchronous `PQexecParams` call is **not actively cancelled
by a signal**. In the isolated lock-wait test it returns SQLSTATE 57014 at
PostgreSQL's configured five-second statement timeout; this is not proof of
prompt SQL cancellation or a bound on all network failure modes. Subsequent
cooperative cancellation checks prevent further work where supported.

## Isolated validation

```sh
python3 tests/pgvn/run_linux_admin.py --sdk build/pgvn/linux-sdk \
  --configuration Release
python3 tests/pgvn/run_linux_admin.py --sdk build/pgvn/linux-sdk \
  --configuration Debug --sanitize
```

The runner uses local builder and PostgreSQL 18.6 images, records their image
IDs and verifies the database version. The disposable database has tmpfs
storage, no published port and no external network. Trust authentication is
restricted to this test fixture, never a deployment recommendation. Runtime,
generated inputs, source and SDK provenance are frozen under
`artifacts/pgvn/linux-admin/`; reports retain `complete_product: false`.
The disposable test container uses `SYS_PTRACE` only to verify its own child
is polling stdin before sending a signal. This is not a runtime capability or
a production deployment requirement. No Supabase or other shared database is
used.

The final Release and ASan/UBSan runs each passed 38 checks; see the
[evidence index](evidence/linux-native-admin.json). This includes CLI-only
schema/grant/repository/credential provisioning, real authenticated SVN
operations, binary/Unicode dump round trips, token-hash verification, protected
key handling, password input rejection, rekey/disable authentication effects,
configuration precedence and stdin signal cancellation. Windows compilation,
the wider protocol matrix and production deployment were not rerun here.
