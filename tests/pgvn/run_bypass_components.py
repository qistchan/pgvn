"""Run isolated PostgreSQL 18.6 + Ubuntu C bypass components, not release gates.

Prepare pgvn-bypass-builder:ubuntu24 with bypass-builder.Dockerfile first.
Requires the managed local Docker daemon. Creates no host ports or volumes;
the named test container and its tmpfs database are removed in finally.
"""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pgvn"))
from build_metadata import collect, sha256, write_metadata
from embed_migrations import CATALOG, STORAGE
from prepare_protocol import prepare_protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image", default="postgres:18.6")
    parser.add_argument("--builder-image", default="pgvn-bypass-builder:ubuntu24")
    args = parser.parse_args()
    run_id = "bypass-" + uuid.uuid4().hex[:12]
    work = ROOT / "build/pgvn/tests" / run_id
    artifacts = ROOT / "artifacts/pgvn/tests" / run_id
    work.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    environment = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    checks = []
    image_ids = {}
    created = False
    error = None
    cleanup_ok = False
    pinned_metadata = None

    def command(arguments, data=None, expected=0, timeout=60):
        result = subprocess.run(arguments, cwd=ROOT, env=environment, input=data, text=True,
                                capture_output=True, timeout=timeout)
        with (artifacts / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"args": arguments, "exit_code": result.returncode,
                                     "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if result.returncode != expected:
            raise RuntimeError("Component command failed: " + str(arguments) + "\n" + result.stdout + result.stderr)
        return result.stdout.strip()

    def sql(statement, expected=0):
        return command(docker + ["exec", "-i", run_id, "psql", "-h", "127.0.0.1", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-U", "postgres", "-d", "pgvn_component"],
                       data=statement, expected=expected)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    try:
        for image in (args.postgres_image, args.builder_image):
            image_ids[image] = command(docker + ["image", "inspect", "--format", "{{.Id}}", image])
        pinned_metadata = write_metadata(ROOT, work / "generated", "linux", "Release")
        upstream = ROOT / "vendor/subversion-1.14.5/subversion"
        outputs = prepare_protocol(upstream, work / "upstream", ROOT / "patches/pgvn/subversion-1.14.5/protocol.json")
        check("pinned SVN protocol patch prepared with all 32 command guards",
              (work / "upstream/svnserve/serve.c").read_text().count("err = pgvn_svn_command(") == 32)
        command(docker + ["run", "-d", "--name", run_id, "--label", "pgvn.component=bypass", "--network", "none",
                          "--memory", "256m", "--cpus", "1", "--read-only",
                          "--tmpfs", "/var/lib/postgresql:rw,nosuid,noexec,size=256m",
                          "--tmpfs", "/var/run/postgresql:rw,nosuid,noexec,size=16m", "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
                          "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=pgvn_component",
                          args.postgres_image, "postgres", "-c", "shared_buffers=16MB", "-c", "max_connections=10"])
        created = True
        deadline = time.monotonic() + 45
        while True:
            # The image's initialization server accepts Unix sockets before it
            # restarts. TCP readiness identifies the final server only.
            result = subprocess.run(docker + ["exec", run_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "pgvn_component"],
                                    env=environment, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Isolated PostgreSQL did not become ready")
            time.sleep(0.2)
        check("PostgreSQL target version", sql("SHOW server_version_num;") == "180006")
        sql("CREATE ROLE pgvn_fixture_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_component TO pgvn_fixture_owner;")
        for namespace, directory, names in (("pgfs", ROOT / "sql", STORAGE), ("pgvn", ROOT / "sql/pgvn", CATALOG)):
            for name in names:
                path = directory / name
                sql("SET ROLE pgvn_fixture_owner; SELECT set_config('pgvn.migration_sha256','" + sha256(path) + "',false);\n" + path.read_text())
                checks.append("migration " + namespace + "/" + name)
        check("all migrations applied by a non-superuser", sql("SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.schema_versions;") == "1,2,3,4,5,6,7")
        cases = [({}, True), ({"svn_authz_mode": "enforce"}, True), ({"svn_authz_mode": "bypass"}, True),
                 ({"svn_authz_mode": "bypass", "anonymous_access": "none", "authenticated_access": "none"}, True),
                 ({"svn_authz_mode": "bypass", "anonymous_access": "read"}, False),
                 ({"svn_authz_mode": "bypass", "anonymous_access": "write"}, False),
                 ({"svn_authz_mode": "unknown"}, False), ({"svn_authz_mode": True}, False),
                 ({"svn_authz_mode": None}, False), ({"unknown": "enforce"}, False),
                 ({"anonymous_access": "read", "authenticated_access": "none"}, True),
                 ({"identity_domain": "身份域", "svn_authz_mode": "bypass"}, True)]
        for number, (settings, accepted) in enumerate(cases, 1):
            literal = json.dumps(settings, ensure_ascii=False).replace("'", "''")
            actual = sql("SELECT pgvn.valid_settings('" + literal + "'::jsonb,true);")
            check("protocol settings case " + str(number), actual == ("t" if accepted else "f"))
        check("old validator rejects both explicit modes", sql("SELECT NOT pgvn.valid_settings('{\"svn_authz_mode\":\"bypass\"}') AND NOT pgvn.valid_settings('{\"svn_authz_mode\":\"enforce\"}');") == "t")
        check("old validator retains legacy defaults", sql("SELECT pgvn.valid_settings('{}') AND pgvn.valid_settings('{\"anonymous_access\":\"read\"}');") == "t")
        check("null capability cannot enable a mode", sql("SELECT NOT pgvn.valid_settings('{\"svn_authz_mode\":\"bypass\"}',NULL);") == "t")
        fixture = """SET ROLE pgvn_fixture_owner;
BEGIN;
INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version) VALUES
('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333',1);
INSERT INTO pgvn.repositories(repository_id,canonical_name,generation) VALUES('11111111-1111-4111-8111-111111111111','test',1);
INSERT INTO pgvn.configurations(repository_id,generation,authz_text,settings,published_by)
SELECT '11111111-1111-4111-8111-111111111111',generation,E'[/]\\n* =\\n','{"identity_domain":"pgvn","svn_authz_mode":"bypass","authenticated_access":"none"}','fixture' FROM generate_series(1,2) generation;
INSERT INTO pgvn.routes VALUES('/test','11111111-1111-4111-8111-111111111111');
INSERT INTO pgvn.credentials(identity_domain,username,key_id,nonce,ciphertext,authentication_tag)
VALUES('pgvn','alice','fixture',decode(repeat('00',12),'hex'),decode('01','hex'),decode(repeat('00',16),'hex'));
COMMIT;
"""
        sql(fixture)
        check("monitor accepts the supported mode", sql("SELECT healthy FROM pgvn_monitor.configuration_health;") == "t")
        check("monitor exposes the native mode", sql("SELECT svn_authz_mode FROM pgvn_monitor.repositories;") == "bypass")
        container_work = "/src/" + work.relative_to(ROOT).as_posix()
        build_script = """set -eu
gcc -std=c17 -D_POSIX_C_SOURCE=200809L -pthread -O0 -g -Wall -Wextra -Werror -Wno-unused-parameter -ffunction-sections -fdata-sections \
    -I "$PGVN_COMPONENT_WORK/generated" -I /src/src/pgvn/core -I /src/src/pgvn/serve \
    -I "$PGVN_COMPONENT_WORK/upstream/svnserve" -I /src/src/libsvn_fs_pg \
    -I /src/vendor/subversion-1.14.5/subversion/include -I /usr/include/subversion-1 \
    $(pkg-config --cflags apr-1 apr-util-1 libpq) \
    /src/tests/pgvn/bypass_probe.c /src/src/pgvn/core/policy.c /src/src/pgvn/core/policy_protocol.c \
    /src/src/pgvn/core/catalog.c /src/src/pgvn/core/context.c /src/src/pgvn/serve/protocol.c \
    -Wl,--gc-sections -lsvn_repos-1 -lsvn_subr-1 $(pkg-config --libs apr-1 apr-util-1 libpq) -o /tmp/bypass-probe
node --check /src/web/pgvn-monitor/monitor.js
sha256sum /tmp/bypass-probe
/tmp/bypass-probe
"""
        output = command(docker + ["run", "--rm", "--network", "container:" + run_id,
                                   "--mount", "type=bind,src=" + str(ROOT) + ",dst=/src,readonly",
                                   "-e", "PGVN_COMPONENT_WORK=" + container_work, args.builder_image,
                                   "sh", "-c", build_script], timeout=90)
        probe = json.loads(output.splitlines()[-1])
        probe["binary_sha256"] = output.splitlines()[0].split()[0]
        check("production policy/protocol C component", probe["status"] == "PASS")
        check("mode publication reached generation 2", sql("SELECT generation FROM pgvn.repositories;") == "2")
        check("guard checks created no revisions", sql("SELECT head_rev FROM pgfs.repositories;") == "0")
        check("mode restored to enforce", sql("SELECT svn_authz_mode FROM pgvn_monitor.repositories;") == "enforce")
        check("build inputs unchanged during validation", collect(ROOT, "linux", "Release") == pinned_metadata)
        (artifacts / "probe.json").write_text(json.dumps(probe, indent=2) + "\n")
        (artifacts / "prepared-protocol.json").write_text(json.dumps(outputs, indent=2) + "\n")
    except Exception:
        error = traceback.format_exc()
        print(error, flush=True)
    finally:
        if created:
            try:
                (artifacts / "postgres.log").write_text(command(docker + ["logs", run_id]), encoding="utf-8")
                command(docker + ["rm", "-f", run_id])
                cleanup_ok = True
            except Exception:
                error = (error or "") + traceback.format_exc()
    report = {"test_id": run_id, "status": "PASS" if error is None else "FAIL", "milestone_complete": False,
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": checks, "error": error,
              "build_identity": (pinned_metadata or collect(ROOT, "linux", "Release"))["build_identity"], "images": image_ids,
              "container_removed": cleanup_ok, "raw_directory": str(artifacts),
              "scope": "Isolated SQL migrations and production POSIX platform/context/policy/protocol components; no SVN wire client, full Linux runtime, real credential crypto or Windows acceptance."}
    (artifacts / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (ROOT / "artifacts/pgvn/tests/bypass-latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"test_id": run_id, "status": report["status"], "checks": len(checks), "container_removed": cleanup_ok}), flush=True)
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
