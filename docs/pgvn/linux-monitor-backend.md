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

The isolated acceptance runner uses the pinned local builder and PostgreSQL 18.6 images. It builds the admin CLI as a test prerequisite to provision the disposable database through PgVN's real CLI, starts PostgreSQL with no published ports and an isolated network, and joins the probe only to that database's network namespace:

```sh
python3 tests/pgvn/run_linux_monitor.py \
  --sdk build/pgvn/linux-sdk --configuration Release
python3 tests/pgvn/run_linux_monitor.py \
  --sdk build/pgvn/linux-sdk --configuration Debug --sanitize
```

The runner freezes source, generated inputs, SDK manifest, build manifests, and products; checks process exit codes and sanitizer output; and removes its uniquely named containers on exit. It does not use a shared or Supabase database. Each run writes a new directory under `artifacts/pgvn/linux-monitor/`, including `report.json`, `process.json`, `metrics.prom`, and `reproducibility.md`. The current evidence index points to the passing runs.

## Runtime and metric limits

- Process CPU time uses `CLOCK_PROCESS_CPUTIME_ID`, converted to 100-nanosecond units, and CPU percentage is normalized by `_SC_NPROCESSORS_ONLN`. This is the online host CPU count, not a cgroup CPU quota.
- Linux RSS is an approximation from resident pages in `/proc/self/statm` multiplied by `_SC_PAGESIZE`, with overflow checks. It is not a precise private-memory measurement.
- Private committed bytes are unavailable on Linux. JSON and series values remain `null`, and Prometheus omits `pgvn_process_private_bytes`; RSS and virtual size are not substituted.
- POSIX sampling uses a cancellable monotonic sleep in chunks of at most 100 ms. Query polling uses `poll()` and preserves the existing query deadline and stop checks. Connection polling has a 3-second monotonic deadline after `PQconnectStartParams`; the initial DNS resolution can still block.
- Monotonic-clock failures fail closed. Windows runtime paths remain guarded, but Windows behavior was not regression-tested in this Linux phase.

The acceptance runs validate bootstrap/readiness, production API query handling and token authorization, database outage/recovery, stale-tick recovery, resource output, private-byte behavior, and bounded shutdown during an observable stalled query. This is component evidence only; it makes no deployment or performance claim.

## Evidence

Release and Debug + ASan/UBSan each passed **23 checks** against PostgreSQL `server_version_num=180006`. The frozen reports retain the actual dirty build identity and their source, generated-input, SDK, and product provenance:

- [Monitor backend evidence index](evidence/linux-monitor-backend.json)
- `artifacts/pgvn/linux-monitor/linux-monitor-4e10b4e266e0/` — Release
- `artifacts/pgvn/linux-monitor/linux-monitor-fa3241fe6d3b/` — Debug + ASan/UBSan
- [Linux development plan checkpoint](../plans/2026-10-17-pgvn-linux-bypass-performance.zh-CN.md#linux-monitor-backend)
