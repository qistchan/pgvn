"""Pinned Linux hook declaration/mapping checks against an isolated PostgreSQL.

Process dispatch is an explicit failing adapter: this does not execute hooks.
"""
from pathlib import Path
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pgvn"))
from build_metadata import collect, sha256, write_metadata
from embed_migrations import CATALOG, STORAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--builder-image", default="pgvn-linux-builder:ubuntu24")
    parser.add_argument("--postgres-image", default="postgres:18.6")
    args = parser.parse_args()
    sdk = args.sdk.resolve(strict=True)
    run_id = "linux-hook-config-" + uuid.uuid4().hex[:12]
    artifacts = ROOT / "artifacts/pgvn/tests" / run_id
    artifacts.mkdir(parents=True)
    metadata = write_metadata(ROOT, artifacts / "inputs", "linux", "Release")
    for relative, expected in metadata["sources"].items():
        target = artifacts / "inputs/source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
        if sha256(target) != expected:
            raise ValueError("Source changed while freezing inputs: " + relative)
    environment = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    checks, probes, builds, images, failures = [], {}, {}, {}, []
    created = cleaned = False

    def command(arguments, data=None, timeout=180):
        result = subprocess.run([str(a) for a in arguments], cwd=ROOT, env=environment,
                                input=data, text=True, capture_output=True, timeout=timeout)
        with (artifacts / "commands.jsonl").open("a") as log:
            log.write(json.dumps({"args": [str(a) for a in arguments], "exit_code": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if result.returncode:
            raise RuntimeError("Hook configuration command failed: " + str(arguments) + "\n" + result.stdout + result.stderr)
        return result.stdout.strip()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    def builder(arguments, compile=False):
        # X_OK must observe an executable mount for availability checks. Probe
        # fixtures are data only; this component never executes them.
        flags = [] if compile else ["--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=32m"]
        return command(docker + ["run", "--rm", "--network", "none" if compile else "container:" + run_id,
                                 "--user", str(os.getuid()) + ":" + str(os.getgid()),
                                 "--memory", "768m", "--cpus", "2", *flags,
                                 "--mount", "type=bind,src=" + str(ROOT) + ",dst=" + str(ROOT) + ("" if compile else ",readonly"),
                                 "-w", ROOT, "-e", "PGVN_HOOK_CONFIG_DSN=host=127.0.0.1 dbname=pgvn_hooks user=hook_owner",
                                 "-e", "ASAN_OPTIONS=detect_leaks=1:abort_on_error=1",
                                 "-e", "UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1", args.builder_image, *arguments])

    def sql(statement):
        return command(docker + ["exec", "-i", run_id, "psql", "-h", "127.0.0.1", "-X", "-v", "ON_ERROR_STOP=1",
                                 "-At", "-U", "postgres", "-d", "pgvn_hooks"], data=statement)

    def reset():
        sql("""SET ROLE hook_owner;
BEGIN;
TRUNCATE pgvn.routes,pgvn.configurations,pgvn.repositories,pgfs.repositories CASCADE;
INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version) VALUES
('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333',1);
INSERT INTO pgvn.repositories(repository_id,canonical_name,generation) VALUES('11111111-1111-4111-8111-111111111111','test',1);
INSERT INTO pgvn.configurations(repository_id,generation,authz_text,hooks,published_by) VALUES
('11111111-1111-4111-8111-111111111111',1,E'[/]\\n* = rw\\n','{"pre-commit":"allow"}','fixture');
INSERT INTO pgvn.routes VALUES('/test','11111111-1111-4111-8111-111111111111');
COMMIT;""")

    try:
        for image in (args.builder_image, args.postgres_image):
            images[image] = command(docker + ["image", "inspect", "--format", "{{.Id}}", image])
        for configuration, sanitize, label in (("Release", False, "release"), ("Debug", True, "sanitized")):
            arguments = ["python3", "scripts/pgvn/build.py", "--target", "linux", "--components", "hook-config",
                         "--configuration", configuration, "--linux-sdk", sdk]
            if sanitize:
                arguments.append("--sanitize")
            result = json.loads(builder(arguments, compile=True))
            output = Path(result["output"])
            # Run the frozen product, preserving its relative runtime search path.
            target = artifacts / "products" / label
            shutil.copytree(output, target, ignore=shutil.ignore_patterns("*.o", "commands.jsonl"))
            builds[label] = json.loads((target / "build-manifest.json").read_text())
            check(label + " frozen product hashes", all(sha256(target / relative) == expected
                  for relative, expected in builds[label]["files"].items()))
        command(docker + ["run", "-d", "--name", run_id, "--label", "pgvn.component=linux-hook-config", "--network", "none",
                          "--read-only", "--memory", "256m", "--cpus", "1",
                          "--tmpfs", "/var/lib/postgresql:rw,nosuid,noexec,size=256m",
                          "--tmpfs", "/var/run/postgresql:rw,nosuid,noexec,size=16m", "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
                          "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=pgvn_hooks", args.postgres_image,
                          "postgres", "-c", "shared_buffers=16MB", "-c", "max_connections=16"])
        created = True
        deadline = time.monotonic() + 45
        while True:
            ready = subprocess.run(docker + ["exec", run_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "pgvn_hooks"],
                                   env=environment, capture_output=True, timeout=5)
            if not ready.returncode:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL TCP readiness timed out")
            time.sleep(.2)
        check("PostgreSQL 18.6", sql("SHOW server_version_num;") == "180006")
        sql("CREATE ROLE hook_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_hooks TO hook_owner;")
        for directory, names in ((ROOT / "sql", STORAGE), (ROOT / "sql/pgvn", CATALOG)):
            for name in names:
                path = directory / name
                sql("SET ROLE hook_owner; SELECT set_config('pgvn.migration_sha256','" + sha256(path) + "',false);\n" + path.read_text())
                checks.append("migration " + name)
        check("schema 8 migrated by non-superuser", sql("SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.schema_versions;") == "1,2,3,4,5,6,7,8")
        base = {"version": 1, "name": "check-policy"}
        cases = [(None, False), ([], False), ({}, True), ({"pre-commit": "allow"}, True),
                 ({"pre-commit": "reject"}, True), ({"pre-commit": {"executable": "old.exe"}}, True),
                 ({"pre-commit": base}, True), ({"pre-commit": {**base, "arguments": ["", "中文", "& ; $(literal)"]}}, True)]
        bad = [{**base, "version": value} for value in (None, True, "1", 0, 2, 1.5)]
        bad += [{**base, "name": value} for value in (None, True, "", "../check", "check.exe", "check policy", "Check", "中文", "x" * 65)]
        bad += [{**base, "executable": "injected.exe"}, {"version": 1}, {"name": "check"},
                {**base, "unknown": "value"}, {**base, "arguments": "bad"}, {**base, "arguments": [1]},
                {**base, "arguments": [""] * 17}, {**base, "arguments": ["中" * 683]},
                {**base, "arguments": ["x" * 2049]}, {**base, "timeout_seconds": 0},
                {**base, "timeout_seconds": 61}, {**base, "timeout_seconds": "30"}]
        cases += [({"pre-commit": handler}, False) for handler in bad]
        cases += [({"unknown-event": base}, False), ({"pre-commit": {**base, "arguments": ["x" * 2048] * 7}}, True),
                  ({"pre-commit": {**base, "arguments": ["x" * 2048] * 8}}, False)]
        for index, (definitions, accepted) in enumerate(cases, 1):
            literal = json.dumps(definitions, ensure_ascii=False).replace("'", "''")
            check("SQL declaration case " + str(index), sql("SELECT pgvn.valid_hooks('" + literal + "'::jsonb,true);") == ("t" if accepted else "f"))
        for capability in ("false", "NULL"):
            check("unsupported declaration capability " + capability,
                  sql("SELECT NOT pgvn.valid_hooks('{\"pre-commit\":{\"version\":1,\"name\":\"check-policy\"}}'," + capability + ");") == "t")
        check("old validator fails closed for new declarations", sql("SELECT NOT pgvn.valid_hooks('{\"pre-commit\":{\"version\":1,\"name\":\"check-policy\"}}');") == "t")
        check("legacy validator remains unchanged", sql("SELECT pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"old.exe\"}}') AND NOT pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"bad.cmd\"}}');") == "t")
        for label in builds:
            reset()
            target = artifacts / "products" / label
            output = builder(["sh", "-c", 'pgvn_hook_tmp=$(mktemp -d /tmp/pgvn-钩子.XXXXXX); trap \'rmdir "$pgvn_hook_tmp"\' EXIT; "$1" "$pgvn_hook_tmp"',
                              "probe", target / "bin/hook_config_probe"])
            probes[label] = json.loads(output)
            check(label + " production policy/configuration probe", probes[label]["status"] == "PASS")
            runtime = json.loads(builder([target / "bin/hook_config_probe", "--version"]))
            check(label + " frozen runtime identity", runtime == builds[label]["runtime"])
            dependencies = builder(["ldd", target / "bin/hook_config_probe"])
            (target / "frozen-ldd.txt").write_text(dependencies + "\n")
            principal = [line for line in dependencies.splitlines()
                         if any(name in line for name in ("libapr-1", "libaprutil-1", "libsvn_", "libpq"))]
            check(label + " frozen runtime uses pinned principal libraries", bool(principal) and
                  all(Path(line.split("=>", 1)[1].split()[0]).resolve().is_relative_to((target / "lib").resolve())
                      for line in principal) and "not found" not in dependencies)
            sql("SET ROLE hook_owner; UPDATE pgvn.configurations SET hooks='{\"pre-commit\":{\"version\":1,\"name\":\"check-policy\"}}' WHERE generation=4;")
            check(label + " monitor accepts declaration schema", sql("SELECT healthy FROM pgvn_monitor.configuration_health;") == "t")
            sql("SET ROLE hook_owner; UPDATE pgvn.configurations SET hooks='{\"pre-commit\":{\"version\":2,\"name\":\"check-policy\"}}' WHERE generation=4;")
            check(label + " monitor rejects unknown declaration version", sql("SELECT healthy FROM pgvn_monitor.configuration_health;") == "f")
        check("source and migrations unchanged during verification", collect(ROOT, "linux", "Release") == metadata)
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
                  "status": "PASS" if not failures else "FAIL", "milestone_complete": False,
                  "scope": "Hook declaration/mapping and production policy loading/publication; process dispatcher is a failing adapter",
                  "build_identity": metadata["build_identity"], "images": images, "builds": builds, "probes": probes,
                  "checks": checks, "failures": failures, "container_removed": cleaned,
                  "source_snapshot": str(artifacts / "inputs"), "sdk_manifest_sha256": sha256(sdk / "sdk-manifest.json"),
                  "limitations": ["No POSIX hook process, argv/environment, output, timeout, cancellation or descendant containment acceptance",
                                  "Availability preflight is not the launch-time executable validation; no complete Linux service or Windows W1/W2 acceptance"]}
        path = artifacts / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        (artifacts.parent / "linux-hook-config-latest.json").write_text(json.dumps({"report": str(path), "status": report["status"]}) + "\n")
        print(json.dumps({"report": str(path), "status": report["status"], "checks": len(checks), "probes": probes, "container_removed": cleaned}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
