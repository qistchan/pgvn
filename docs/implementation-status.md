# PgFS implementation status

Native Windows SVN service access and the current compatibility gate are
verified, including the authorized fixed-history Polarion repository comparison.
The native SVN compatibility and service milestone is complete within the
explicit platform and test scope below. Actual Polarion application integration,
representative performance and PostgreSQL HA follow this milestone.

## Current backend

The C/APR/libpq provider builds as `runtime/svn/bin/libsvn_fs_pg-1.dll` against
the SVN 1.14.5 private FS interface. There are 77 bound error-returning callbacks
and one callback delegated to the loader's changed-path iterator emulation.
Storage-specific BDB operations return unsupported errors; unknown ioctls
return the standard unrecognized-ioctl error. Callback coverage does not prove
universal compatibility with every SVN version or another backend's format.

SQL migrations 1–4 store independent repository identities, immutable nodes,
directory overlays, property sets, 1 MiB fulltext chunks, staging transactions,
published revisions, changes, locks and incremental-hotcopy mappings. Revision
allocation is HEAD + 1 under the repository row lock. Object sequences may
have gaps; successful revision numbers do not.

Staging locks the transaction. Publication locks the repository then the
transaction, merges against HEAD, checks locks and atomically publishes roots,
changes, properties and the commit receipt. Fulltexts attach only after stream
close and checksum verification. A fresh connection resolves a lost COMMIT
response using the persistent receipt.

One file text writer may be open per SVN transaction, enforced across processes
by a PostgreSQL session advisory lock and within a connection by a writer map.
Publication rejects an open writer. Process/connection death releases the
lease; a later writer or commit abandons incomplete representations. Partial
content remains invisible, and closed staged content persists through crashes.

Copies share descendants. Mutation inherits copy context; transaction node IDs
remain stable. Native history follows copy ancestry. Changed-path folding
preserves add/replace and copyfrom metadata across subsequent modifications.

## Reproducible evidence

Current module SHA-256: `a06a79ba5efb9cf9bace026fdda1dfe820e3df13f23ae1f3f08403a2b121bb5c`.

The complete audit is `artifacts/tests/compatibility-evidence.json`.
It validates module/native-source hashes, exact executed test IDs, native test
binaries, pinned source integrity and every generated historical snapshot.

| Evidence | Current result |
| --- | --- |
| Custom integration checks | 33 passed: loader, staging, streams, CAS, copy/history, mergeinfo, locks, administration, connection faults, crash recovery, protocols and generated history |
| Apache native FS/repos checks | 94 passed; 4 upstream skips; 4 storage/version-specific exclusions |
| Apache command-line/protocol matrix | 51 complete applicable suite runs; 1891 PASS, 69 SKIP, 54 upstream-marked XFAIL; no FAIL or XPASS |
| Generated ALM-style repository | All 20 revisions verified and compared to FSFS |
| Authorized real Polarion history | r0-r637: 638 revisions verified; all changed-path records and revision properties match; 5 complete logical snapshots match |
| Upstream source integrity | 1195 C/header/Python files match the pinned archive |

The upstream execution count includes repeated cases over different protocols.
It is not a count of unique test definitions. Each skip, expected failure and
exclusion is retained in the audit instead of counted as a passing test.

The matrix includes basic/copy/commit/log, merge variants and mergeinfo,
update/switch/diff/blame, properties and inherited properties, locks,
checkout/depth/externals/import/export, history/info/cat, svnlook/svnversion,
svndumpfilter/svnmucc, svnadmin, authz, svnsync and svnrdump. Protocol-specific
checks include mod_dav_svn, mod_authz_svn and authorization-sensitive merge,
sync and inherited-property operations over both svnserve and HTTP.

Native FS exclusions are 40 (FSFS/BDB format-number assumptions), 65 and 67
(FSFS/FSX SQLite rep-cache and representation-file operations). Native repos
21 assumes any non-FSX backend can create a pre-1.4 native repository. PgFS
uses format 1 and reports SVN 1.14 as its minimum native compatibility.
svnadmin 43 directly corrupts an FSFS revision file, which PgFS does not use.
PgFS corruption detection is separately tested by corrupting its actual
PostgreSQL fulltext storage. Native skips include FSFS upgrade/configuration,
a module-name test that only recognizes built-in FS names, and optional authz
performance. Original assertions and test guards are unchanged.

The generated corpus contains UTF-8 work-item XML, directories, binary and
empty attachments, binary properties, mergeinfo, historical/nested copies,
replacement, moves, copy-source deletion and same-path resurrection. Every
revision comparison includes node kind/created/origin/copy metadata, actual
file-stream SHA1 and length, properties, changed paths and revision properties.

Custom protocol checks use the untouched Polarion SVN 1.14.5 client and the
installed SVNKit 1.10.1 bundle, with their hashes recorded. Both protocols
commit/read binary data and properties and exercise authorization and hooks.
A separate check starts two independent svnserve processes on one repository.
Commits and mutable revision properties are immediately visible in both
directions; locks remain enforced across frontends and restricted reads stay
denied. The actual Polarion application is not started or migrated by these tests.

The real Polarion repository was exported read-only at the fixed HEAD r637
and loaded into an independent repository under `runtime/corpus`, backed by the
separate development database `pgfs_poc`. Its 61,641,751-byte dump
is retained with a SHA-256 digest. All 638 revisions pass
`svnadmin verify`; all changed-path records and all revision properties match.
Revision properties are checked against the fixed dump to avoid assuming live
revision properties never change. Complete tree/content/property/history-metadata
snapshots match at revisions 0, 1, 318, 636, 637.
The complete real tree was sampled at those revisions; it was not compared at
every revision. Original repository content, configuration and services were
not changed by this validation.

## Windows build and recovery

The stock supplied FS loader disables third-party provider names. An isolated
loader is rebuilt from unchanged pinned sources with DSO loading and checked
against all original public FS exports. Polarion files are not replaced.

The supplied subr DLL exports `svn_ctype_table` as an array rather than the
upstream header's pointer. The build probes this ABI and generates only a
consumer-header shim. PE import generation distinguishes data/function symbols.
An isolated WC DLL rebuilt from unchanged sources exposes private functions
needed by Apache's regression helpers; normal network clients use the original
distribution.

The PostgreSQL 18.6 development cluster is `runtime/pg-primary`, loopback
15432, database `pgfs_poc`. Tests validate its actual data directory before
controlling it and keep fsync/full_page_writes enabled. pg_ctl start/restart
uses log files to avoid inherited output-pipe hangs on Windows. Recovery checks
cover published nonzero revisions and persisted staging after immediate stop.

## Reproduce and connect

```powershell
& .\scripts\initialize-test-db.ps1
python .\scripts\build.py
python .\scripts\build_native_tests.py
python .\tests\run_gates.py
```

The gate runs database restart checks before concurrent suites and performs
the evidence audit last. Tests create distinct synthetic repositories and
terminate their own loopback svnserve/Apache processes.

See [service integration](service-integration.md) for a native svnserve
endpoint, Apache configuration and runtime requirements. The original
[PoC plan](plans/2026-10-14-postgresql-fs-poc.md) is unchanged.

## Later milestones and limits

Actual Polarion application integration, representative performance and
database HA remain later milestones. Full/incremental hotcopy currently uses
one PostgreSQL database; native dump/load supports cross-database transfer.
Fulltexts currently use 1 MiB database chunks without representation delta
compression or deduplication. Whole-repository verification reuses successfully verified immutable nodes
across ascending revisions, checks node revision constraints at each occurrence,
and keeps cache keys outside per-revision pools. Per-revision local checks
follow only newly created nodes, matching the upstream global/local API split;
recovery retains a complete HEAD check. Historical corruption, revision range
behavior and transaction-root validation have dedicated regression checks. Representative throughput,
latency and memory use still require performance evaluation.
Passing this fixed-version gate establishes evidence for the tested scope,
not compatibility with every server ABI, optional feature or deployment.
