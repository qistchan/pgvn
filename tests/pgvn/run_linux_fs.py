"""Build/validate the real Linux FS and compare hex/binary transport in isolation.

Requires a prepared SDK and the local Docker images. No published ports,
production connection strings, persistent DB volumes, or cache-state claims.
"""
from pathlib import Path
import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pgvn"))
from build_metadata import collect, sha256
from embed_migrations import STORAGE


def summarize(samples):
    elapsed = sorted(s["elapsed_ms"] for s in samples)
    return {"operations": len(samples), "p50_ms": statistics.median(elapsed),
            "p95_ms": elapsed[math.ceil(len(elapsed) * .95) - 1],
            "p99_ms": elapsed[math.ceil(len(elapsed) * .99) - 1],
            "mean_cpu_ms": statistics.mean(s["cpu_ms"] for s in samples),
            "queries_per_operation": sorted(set(s["queries"] for s in samples)),
            "chunk_column_bytes_per_operation": sorted(set(s["chunk_column_bytes"] for s in samples))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--builder-image", default="pgvn-linux-builder:ubuntu24")
    parser.add_argument("--postgres-image", default="postgres:18.6")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 8, 32])
    args = parser.parse_args()
    if args.rounds < 3 or not 1 <= args.repetitions <= 100 or any(c not in (1, 8, 32) for c in args.concurrency):
        parser.error("Use at least 3 rounds, 1..100 repetitions and concurrency 1/8/32")
    sdk = args.sdk.resolve(strict=True)
    run_id = "linux-fs-" + uuid.uuid4().hex[:12]
    artifacts = ROOT / "artifacts/pgvn/tests" / run_id
    artifacts.mkdir(parents=True)
    env = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    checks, builds, groups, failures = [], {}, [], []
    images = {}
    created = cleaned = False
    metadata = collect(ROOT, "linux", "Release")
    repository_id = str(uuid.uuid4())

    def command(arguments, data=None, expected=0, timeout=120):
        result = subprocess.run([str(a) for a in arguments], input=data, cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=timeout)
        record = {"args": [str(a) for a in arguments], "exit_code": result.returncode,
                  "stdout": result.stdout, "stderr": result.stderr}
        with (artifacts / "commands.jsonl").open("a") as log:
            log.write(json.dumps(record) + "\n")
        if result.returncode != expected:
            raise RuntimeError("Linux FS command failed: " + str(arguments) + "\n" + result.stdout + result.stderr)
        return (result.stdout + (result.stderr if expected else "")).strip()

    def sql(statement):
        return command(docker + ["exec", "-i", run_id, "psql", "-h", "127.0.0.1", "-X", "-v", "ON_ERROR_STOP=1",
                                 "-At", "-U", "postgres", "-d", "pgvn_fs"], data=statement)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    def in_builder(arguments, shared_network=False, expected=0):
        return command(docker + ["run", "--rm", "--network", "container:" + run_id if shared_network else "none",
                                 "--cpus", "4", "--memory", "1g", "--user", str(os.getuid())+":"+str(os.getgid()),
                                 "--mount", "type=bind,src=" + str(ROOT) + ",dst=" + str(ROOT),
                                 "-w", ROOT, "-e", "PGVN_FS_TEST_DSN=host=127.0.0.1 dbname=pgvn_fs user=pgvn_fixture_owner",
                                 "-e", "PGVN_FS_TEST_REPOSITORY=" + repository_id,
                                 args.builder_image, *arguments], expected=expected, timeout=180)

    def probe(variant, operation, *arguments, expected=0):
        return in_builder([ROOT / "build/pgvn/linux" / ("release-" + variant) / "bin/fs_linux_probe",
                           operation, *arguments], shared_network=True, expected=expected)

    try:
        for image in (args.builder_image, args.postgres_image):
            images[image] = command(docker + ["image", "inspect", "--format", "{{.Id}}", image])
        for variant in ("hex", "binary", "binary-per-node"):
            output = in_builder(["python3", "scripts/pgvn/build.py", "--target", "linux", "--components", "fs",
                                 "--configuration", "Release", "--linux-sdk", sdk, "--fs-content-transfer", "hex" if variant=="hex" else "binary",
                                 "--fs-directory-mode", "per-node" if variant.endswith("per-node") else "batch"])
            builds[variant] = json.loads(Path(json.loads(output)["manifest"]).read_text())
            check(variant + " ELF build with pinned principal dependencies", builds[variant]["runtime"]["libpq_version"] == 180006)
        relocated = artifacts / "relocated"
        shutil.copytree(ROOT / "build/pgvn/linux/release-binary", relocated, symlinks=True)
        relocation_script = """import json,pathlib,subprocess,sys
p=pathlib.Path(sys.argv[1])
runtime=json.loads(subprocess.check_output([p/'bin/fs_linux_probe','--version'],text=True))
dependencies=subprocess.check_output(['ldd',p/'lib/libsvn_fs_pg-1.so.0'],text=True)
assert 'not found' not in dependencies and sys.argv[2] not in dependencies
for line in dependencies.splitlines():
 if any(name in line for name in ('libsvn_','libapr-1','libaprutil-1','libpq')):
  assert str(p/'lib') in line,line
print(json.dumps({'runtime':runtime,'dependencies':dependencies}))
"""
        relocation = json.loads(in_builder(["python3", "-c", relocation_script, relocated, sdk]))
        check("relocated ELF resolves bundled principal libraries", relocation["runtime"]["libpq_version"] == 180006)
        command(docker + ["run", "-d", "--name", run_id, "--label", "pgvn.component=linux-fs", "--network", "none",
                          "--cpus", "2", "--memory", "512m", "--read-only",
                          "--tmpfs", "/var/lib/postgresql:rw,nosuid,noexec,size=512m",
                          "--tmpfs", "/var/run/postgresql:rw,nosuid,noexec,size=16m", "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
                          "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=pgvn_fs", args.postgres_image,
                          "postgres", "-c", "shared_buffers=64MB", "-c", "max_connections=48"])
        created = True
        deadline = time.monotonic() + 45
        while True:
            ready = subprocess.run(docker + ["exec", run_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "pgvn_fs"],
                                   env=env, capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL TCP readiness timed out")
            time.sleep(.2)
        check("PostgreSQL 18.6", sql("SHOW server_version_num;") == "180006")
        sql("CREATE ROLE pgvn_fixture_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_fs TO pgvn_fixture_owner;")
        for name in STORAGE:
            path = ROOT / "sql" / name
            sql("SET ROLE pgvn_fixture_owner; SELECT set_config('pgvn.migration_sha256','" + sha256(path) + "',false);\n" + path.read_text())
            checks.append("migration " + name)
        prepared = json.loads(probe("binary", "prepare"))
        check("real FS creates and commits binary dataset", prepared["status"] == "PASS")
        for variant in ("hex", "binary"):
            output = json.loads(probe(variant, "check"))
            check(variant + " reads boundaries/seeks/IDs/properties/copy/abort/verify", output["status"] == "PASS")
            builds[variant]["functional_probe"] = output
        # Keep a real chunk on a separate table for restoration. Fault injection
        # is completed before measurements and never shares a timed operation.
        target = "(SELECT content_id FROM pgfs.contents WHERE length=1048575)"
        sql("CREATE TABLE public.saved_chunk AS SELECT * FROM pgfs.content_chunks WHERE content_id=" + target + ";")
        for fault, change in (("missing", "DELETE FROM pgfs.content_chunks WHERE content_id=" + target),
                              ("truncated", "UPDATE pgfs.content_chunks SET data=substring(data FROM 1 FOR 1) WHERE content_id=" + target)):
            sql(change + ";")
            for variant in ("hex", "binary"):
                check(variant + " rejects " + fault + " chunk", "PgFS content chunk" in probe(variant, "read", expected=1))
            sql("DELETE FROM pgfs.content_chunks WHERE content_id=" + target + "; INSERT INTO pgfs.content_chunks SELECT * FROM public.saved_chunk;")
        check("restored data is intact", json.loads(probe("binary", "check"))["status"] == "PASS")
        for concurrency in args.concurrency:
            for round_number in range(args.rounds):
                order = ("hex", "binary") if round_number % 2 == 0 else ("binary", "hex")
                for variant in order:
                    # Each worker warms the same committed PG data once, then
                    # records repeated operations over its own FS connection.
                    start = time.monotonic()
                    script = """import concurrent.futures,json,os,subprocess,sys,threading,time
phase=[0.0]
barrier=threading.Barrier(int(sys.argv[2]),action=lambda:phase.__setitem__(0,time.monotonic()))
def worker(i):
 p=subprocess.Popen([sys.argv[1],sys.argv[4] if len(sys.argv)>4 else 'bench',sys.argv[3]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PGVN_FS_BENCH_SYNC='1'))
 try:
  warmup=p.stdout.readline()
  json.loads(warmup)
  barrier.wait(timeout=30)
  out,err=p.communicate('G',timeout=90)
  if p.returncode: raise RuntimeError(err)
  return [json.loads(line) for line in out.splitlines()]
 finally:
  if p.poll() is None: p.kill();p.wait()
with concurrent.futures.ThreadPoolExecutor(max_workers=int(sys.argv[2])) as pool:
 rows=list(pool.map(worker,range(int(sys.argv[2]))))
print(json.dumps({'workers':rows,'phase_elapsed_seconds':time.monotonic()-phase[0]}))
"""
                    binary = ROOT / "build/pgvn/linux" / ("release-" + variant) / "bin/fs_linux_probe"
                    measurement = json.loads(in_builder(["python3", "-c", script, binary, str(concurrency), str(args.repetitions + 1)], shared_network=True))
                    samples = [sample for worker in measurement["workers"] for sample in worker]
                    expected = 8388639 * (2 if variant == "hex" else 1)
                    check(f"{variant} concurrency {concurrency} round {round_number+1}: payload and result integrity",
                          len(samples) == concurrency * args.repetitions and all(s["chunk_column_bytes"] == expected for s in samples))
                    group = {"workload": "file-8MiB", "variant": variant, "concurrency": concurrency, "round": round_number + 1,
                             "samples": samples, "summary": summarize(samples),
                             "phase_elapsed_seconds": measurement["phase_elapsed_seconds"],
                             "operations_per_second": len(samples) / measurement["phase_elapsed_seconds"],
                             "harness_elapsed_seconds": time.monotonic() - start}
                    groups.append(group)
        for concurrency in args.concurrency:
            for round_number in range(args.rounds):
                order=("binary-per-node","binary") if round_number%2==0 else ("binary","binary-per-node")
                for variant in order:
                    binary=ROOT/"build/pgvn/linux"/("release-"+variant)/"bin/fs_linux_probe"
                    measurement=json.loads(in_builder(["python3","-c",script,binary,str(concurrency),str(args.repetitions+1),"dirbench"],shared_network=True))
                    samples=[sample for worker in measurement["workers"] for sample in worker]
                    check(f"directory {variant} concurrency {concurrency} round {round_number+1}: all entries and bounded queries",
                          len(samples)==concurrency*args.repetitions and all(s["entries"]==256 and s["queries"]==(260 if variant.endswith("per-node") else 4) for s in samples))
                    groups.append({"workload":"directory-256","variant":variant,"concurrency":concurrency,"round":round_number+1,
                                   "samples":samples,"summary":summarize(samples),"phase_elapsed_seconds":measurement["phase_elapsed_seconds"],
                                   "operations_per_second":len(samples)/measurement["phase_elapsed_seconds"]})
        check("read-only benchmark leaves HEAD at revision 1", sql("SELECT max(rev) FROM pgfs.revisions;") == "1")
        check("source snapshot unchanged", collect(ROOT, "linux", "Release") == metadata)
    except Exception:
        failures.append(traceback.format_exc())
        print(failures[-1], file=sys.stderr, flush=True)
    finally:
        if created:
            try:
                command(docker + ["rm", "-f", run_id])
                cleaned = True
            except Exception:
                failures.append(traceback.format_exc())
        report = {"format": 1, "run_id": run_id, "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "status": "PASS" if not failures else "FAIL", "scope": "Linux FS API / binary transfer and directory batching exploration; no SVN/DAV server acceptance",
                  "build_identity": metadata["build_identity"], "images": images,
                  "sdk_manifest_sha256": sha256(sdk / "sdk-manifest.json"), "builds": builds,
                  "checks": checks, "groups": groups, "failures": failures, "container_removed": cleaned,
                  "method": {"warmup_reads_per_worker": 1, "rounds": args.rounds, "repetitions": args.repetitions,
                             "dataset_bytes": 8388639, "directory_entries":256, "seed": "0x73b198ac integer mixing by byte offset",
                             "cache": "same dataset prewarmed for each worker; no OS/PG cold-cache claim; no Redis/L1",
                             "payload": "sum PQgetlength on content chunk columns, excludes protocol overhead",
                             "limits": "shared container host; local PG only; client CPU includes SHA1; PG CPU/I/O and wire protocols not measured",
                             "resources": "Ubuntu builder 4 CPU/1GiB, PG 2 CPU/512MiB with shared_buffers=64MiB"}}
        path = artifacts / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        (artifacts.parent / "linux-fs-latest.json").write_text(json.dumps({"report": str(path), "status": report["status"]}) + "\n")
        print(json.dumps({"report": str(path), "status": report["status"], "checks": len(checks), "container_removed": cleaned}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
