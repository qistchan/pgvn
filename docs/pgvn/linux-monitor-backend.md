# Linux monitoring data backend

## Scope

The `monitor` component ports the monitoring data backend to POSIX: sampler lifecycle, database polling, resource metrics, readiness data, and the production `pgvn_monitor_api`. Its isolated probe calls that API directly. The component build produces a static backend library and test probe; it does not build an HTTP listener or render the monitoring web UI.

Linux `pgvnserve` telemetry is not enabled or validated by this checkpoint. Run the native service with `--monitor-disabled`. The probe's business-ready flag is synthetic and is not evidence of integration with a live SVN service. Apache/DAV, deployment, and full-product acceptance remain out of scope; `complete_product` remains `false`.

## Build and test

Use the pinned SDK and Ubuntu 24.04 builder environment:

```sh
python3 scripts/pgvn/build.py --target linux --components monitor \
  --configuration Release --linux-sdk build/pgvn/linux-sdk
```

The component output is under `build/pgvn/linux/release-monitor/`. The build generates `pgvn_web.h` for the source/build inputs but does not build an HTTP host or ship a rendered UI. The `linux_monitor_probe` executable is a test-only production-API probe.

The isolated acceptance runner builds the admin CLI as a test prerequisite to provision the disposable database through PgVN's real CLI. It uses the locally managed Docker daemon, starts PostgreSQL 18.6 with no published ports on an isolated network, and joins the probe only to that database's network namespace. PostgreSQL runtime acceptance requires `server_version_num=180006`. The builder image defaults to `pgvn-linux-builder:24.04`; `--builder-image <tag>` selects another locally available builder. `--sdk` accepts absolute or repository-relative paths, but the resolved SDK path must remain inside the repository mounted at `/repo`.

```sh
python3 tests/pgvn/run_linux_monitor.py \
  --sdk build/pgvn/linux-sdk --configuration Release
python3 tests/pgvn/run_linux_monitor.py \
  --sdk build/pgvn/linux-sdk --configuration Debug --sanitize
```

The runner freezes source, generated inputs, SDK manifest, build manifests, and products; checks process exit codes and sanitizer output; and removes its uniquely named containers on exit, verifying that they are absent before reporting PASS. It does not use a shared or Supabase database. Each run writes a new directory under `artifacts/pgvn/linux-monitor/`, including `report.json`, `process.json`, one framed `metrics.prom` response, and `reproducibility.md`. The report records observed image IDs and host/builder OS details rather than accepting fixed image or SDK-manifest digests.

## Runtime and metric limits

- Process CPU time uses `CLOCK_PROCESS_CPUTIME_ID`, converted to 100-nanosecond units, and CPU percentage is normalized by `_SC_NPROCESSORS_ONLN`. This is the online host CPU count, not a cgroup CPU quota.
- Linux RSS is an approximation from resident pages in `/proc/self/statm` multiplied by `_SC_PAGESIZE`, with overflow checks. It is not a precise private-memory measurement.
- Private committed bytes are unavailable through this Linux implementation. JSON and series values remain `null`, and Prometheus omits `pgvn_process_private_bytes`; RSS and virtual size are not substituted.
- POSIX sampling uses a cancellable monotonic sleep in chunks of at most 100 ms. Query polling uses `poll()` and preserves the existing query deadline and stop checks. Connection polling has a 3-second monotonic deadline after `PQconnectStartParams`; the initial DNS resolution can still block.
- Monotonic-clock failures fail closed. Windows runtime paths remain guarded, but Windows behavior was not regression-tested in this Linux phase.

The acceptance runs validate the configured-but-not-business-ready initial state (unconfigured bootstrap is not tested), sampled readiness, stale-tick loss and explicit recovery, individual valid/malformed/duplicate/invalid-UTF-8 API outcomes, token authorization/expiry/revocation, and outage/recovery through fresh samples. The nonempty private-byte series contains only null values. Shutdown is exercised while a sampler query is observably waiting on a relational lock; probe and lock-holder exits are checked and no sampler sessions or locks remain. This is component evidence only; it makes no HTTP, service-integration, deployment, or performance claim.

## Evidence

Release and Debug + ASan/UBSan each passed **39 checks** against PostgreSQL `server_version_num=180006`. The corrected runs observed `pgvn-linux-builder:24.04` (`sha256:44016326629a493a5c2ecd36b82cb05ed136565ec89437531c4a87b36c59efa7`) and `postgres:18.6` (`sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`) with `docker image inspect`. The recorded host was Ubuntu 22.04.5 LTS; the builder was Ubuntu 24.04.5 LTS. SDK manifest SHA-256: `ebefa9ab7454a3a2fb4a1cffcac42ff966787032c3c4f3f309d256e65d8b6055`. The frozen reports retain the actual dirty build identity and their source, generated-input, SDK, and product provenance:

- [Monitor backend evidence index](evidence/linux-monitor-backend.json)
- `artifacts/pgvn/linux-monitor/linux-monitor-0f158271268e/` — Release; 0 sampler sessions/locks after stop; session-release observation 0.526 s
- `artifacts/pgvn/linux-monitor/linux-monitor-1addffb8a852/` — Debug + ASan/UBSan; 0 sampler sessions/locks after stop; session-release observation 0.413 s
- [Linux development plan checkpoint](../plans/2026-10-17-pgvn-linux-bypass-performance.zh-CN.md#linux-monitor-backend)
