"""Isolated Linux credential/permission/SQL checks, including ASan and UBSan.

Uses only the managed local Docker daemon. Needs the pinned Linux SDK and
cryptography for an independent reference codec; never connects a deployment DB.
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

import cryptography
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
    run_id = "linux-credentials-" + uuid.uuid4().hex[:12]
    artifacts = ROOT / "artifacts/pgvn/tests" / run_id
    work = ROOT / "build/pgvn/tests" / run_id
    artifacts.mkdir(parents=True)
    work.mkdir(parents=True)
    metadata = write_metadata(ROOT, artifacts / "inputs", "linux", "Release")
    # Preserve the actual dirty-worktree inputs as well as their inventory.
    for relative, expected in metadata["sources"].items():
        source = ROOT / relative
        target = artifacts / "inputs/source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha256(target) != expected:
            raise ValueError("Source changed while freezing inputs: " + relative)
    environment = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    checks, probes, builds, images, failures = [], {}, {}, {}, []
    created = cleaned = False
    key = bytes(range(32))  # Public, deliberately fixed test key; never deploy.
    nonce = bytes(range(12))
    domain, user, password = "身份域", "用戶é", "pássword/密碼"
    aad = (domain + "\n" + user + "\nvector-v1").encode()
    sealed = AESGCM(key).encrypt(nonce, password.encode(), aad)

    def command(arguments, data=None, timeout=180):
        result = subprocess.run([str(a) for a in arguments], cwd=ROOT, env=environment,
                                input=data, text=True, capture_output=True, timeout=timeout)
        with (artifacts / "commands.jsonl").open("a") as log:
            log.write(json.dumps({"args": [str(a) for a in arguments], "exit_code": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if result.returncode:
            raise RuntimeError("Credential command failed: " + str(arguments) + "\n" + result.stdout + result.stderr)
        return result.stdout.strip()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    def builder(arguments, *, compile=False, network=False, root=False):
        mounts = "type=bind,src=" + str(ROOT) + ",dst=" + str(ROOT) + ("" if compile else ",readonly")
        flags = [] if compile else ["--read-only", "--tmpfs", "/tmp:rw,nosuid,noexec,size=32m"]
        return command(docker + ["run", "--rm", "--network", "container:" + run_id if network else "none",
                                 "--user", "0:0" if root else str(os.getuid()) + ":" + str(os.getgid()),
                                 "--memory", "768m", "--cpus", "2", *flags,
                                 "--mount", mounts, "-w", ROOT,
                                 "-e", "PGVN_CREDENTIAL_DSN=host=127.0.0.1 dbname=pgvn_credentials user=credential_owner",
                                 "-e", "PGVN_CREDENTIAL_READER_DSN=host=127.0.0.1 dbname=pgvn_credentials user=credential_reader",
                                 "-e", "ASAN_OPTIONS=detect_leaks=1:abort_on_error=1",
                                 "-e", "UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1",
                                 args.builder_image, *arguments])

    def sql(statement):
        return command(docker + ["exec", "-i", run_id, "psql", "-h", "127.0.0.1", "-X", "-v", "ON_ERROR_STOP=1",
                                 "-At", "-U", "postgres", "-d", "pgvn_credentials"], data=statement)

    try:
        for image in (args.builder_image, args.postgres_image):
            images[image] = command(docker + ["image", "inspect", "--format", "{{.Id}}", image])
        for configuration, sanitize, label in (("Release", False, "release"), ("Debug", True, "sanitized")):
            arguments = ["python3", "scripts/pgvn/build.py", "--target", "linux", "--components", "credentials",
                         "--configuration", configuration, "--linux-sdk", sdk]
            if sanitize:
                arguments.append("--sanitize")
            result = json.loads(builder(arguments, compile=True))
            manifest = Path(result["manifest"])
            build = json.loads(manifest.read_text())
            builds[label] = build
            shutil.copy2(manifest, artifacts / (label + "-build-manifest.json"))
            runtime = build["runtime"]
            check(label + " actual OpenSSL 3 build/runtime match", runtime["openssl_build"] == runtime["openssl_runtime"] and runtime["openssl_runtime"].startswith("OpenSSL 3."))
            binary = Path(result["output"]) / "bin/credential_probe"
            for operation in ("vectors", "keys", "owner"):
                output = json.loads(builder([binary, operation], root=operation == "owner"))
                probes[label + "_" + operation] = output
                check(label + " " + operation + " probe", output["status"] == "PASS")
        command(docker + ["run", "-d", "--name", run_id, "--label", "pgvn.component=linux-credentials", "--network", "none",
                          "--read-only", "--memory", "256m", "--cpus", "1",
                          "--tmpfs", "/var/lib/postgresql:rw,nosuid,noexec,size=256m",
                          "--tmpfs", "/var/run/postgresql:rw,nosuid,noexec,size=16m", "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
                          "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=pgvn_credentials", args.postgres_image,
                          "postgres", "-c", "shared_buffers=16MB", "-c", "max_connections=16"])
        created = True
        deadline = time.monotonic() + 45
        while True:
            ready = subprocess.run(docker + ["exec", run_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "pgvn_credentials"],
                                   env=environment, capture_output=True, timeout=5)
            if not ready.returncode:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL TCP readiness timed out")
            time.sleep(.2)
        check("PostgreSQL 18.6", sql("SHOW server_version_num;") == "180006")
        sql("CREATE ROLE credential_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; CREATE ROLE credential_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_credentials TO credential_owner;")
        for directory, names in ((ROOT / "sql", STORAGE), (ROOT / "sql/pgvn", CATALOG)):
            for name in names:
                path = directory / name
                sql("SET ROLE credential_owner; SELECT set_config('pgvn.migration_sha256','" + sha256(path) + "',false);\n" + path.read_text())
                checks.append("migration " + name)
        sql("GRANT USAGE ON SCHEMA pgfs,pgvn TO credential_reader; GRANT SELECT ON ALL TABLES IN SCHEMA pgfs,pgvn TO credential_reader; GRANT EXECUTE ON FUNCTION pgvn.guard_credential(text,text,bigint) TO credential_reader;")
        check("independent reference matches frozen UTF-8 vector", sealed.hex() == "37c17768b692ad69e96e7224370edad11285b80ffe554ddf72709a8fcf779450")
        for label, directory in (("release", "release-credentials"), ("sanitized", "debug-credentials-sanitized")):
            # Each DB run starts from exactly the same public vector fixture.
            sql("TRUNCATE pgvn.credentials; INSERT INTO pgvn.credentials(identity_domain,username,key_id,nonce,ciphertext,authentication_tag) VALUES ('身份域','用戶é','vector-v1',decode('" + nonce.hex() + "','hex'),decode('" + sealed[:-16].hex() + "','hex'),decode('" + sealed[-16:].hex() + "','hex'));")
            output = json.loads(builder([ROOT / "build/pgvn/linux" / directory / "bin/credential_probe", "database"], network=True))
            probes[label + "_database"] = output
            check(label + " real credential SQL and concurrent rotation", output["status"] == "PASS")
            record = json.loads(sql("SELECT json_build_object('nonce',encode(nonce,'hex'),'ciphertext',encode(ciphertext,'hex'),'tag',encode(authentication_tag,'hex')) FROM pgvn.credentials WHERE username='reference-write';"))
            plain = AESGCM(key).decrypt(bytes.fromhex(record["nonce"]), bytes.fromhex(record["ciphertext"] + record["tag"]), (domain + "\nreference-write\nvector-v1").encode())
            check(label + " independent reference decrypts production output", plain == password.encode())
            check(label + " reader has no credential UPDATE privilege", sql("SELECT NOT has_table_privilege('credential_reader','pgvn.credentials','UPDATE');") == "t")
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
                  "status": "PASS" if not failures else "FAIL", "scope": "Linux credential and key-file components; Windows/protocol acceptance deferred",
                  "build_identity": metadata["build_identity"], "images": images, "builds": builds, "probes": probes,
                  "checks": checks, "failures": failures, "container_removed": cleaned, "database_created": created,
                  "source_snapshot": str(artifacts / "inputs"), "sdk_manifest_sha256": sha256(sdk / "sdk-manifest.json"),
                  "reference": {"package": "cryptography", "version": cryptography.__version__, "openssl": default_backend().openssl_version_text(),
                                "algorithm": "AESGCM", "key": "public test key 00..1f", "aad": "domain UTF-8 + LF + username UTF-8 + LF + key ID"},
                  "limitations": ["Telemetry sinks are adapters; no complete Linux host or SVN/DAV/CRAM session",
                                  "No Windows execution; fixed vector compatibility does not replace CNG/OpenSSL cross-OS import/rotation acceptance",
                                  "Linux key policy is service EUID ownership, 0400/0600, one regular-file link; group-readable key files are not enabled"]}
        path = artifacts / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        (artifacts.parent / "linux-credentials-latest.json").write_text(json.dumps({"report": str(path), "status": report["status"]}) + "\n")
        print(json.dumps({"report": str(path), "status": report["status"], "checks": len(checks), "probes": probes, "container_removed": cleaned}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
