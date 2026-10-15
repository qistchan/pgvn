"""Directory-free core integration gate against only the dedicated PgVN fixture."""
from pathlib import Path
import concurrent.futures
import datetime as dt
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import time
import traceback
import unittest
import uuid
from fixture import Fixture, ROOT, RUNTIME

TEST_ID = "m1-" + uuid.uuid4().hex[:12]
WORK = RUNTIME / "tests" / TEST_ID
EMPTY = WORK / "pathless-working-directory"
PROBE = RUNTIME / "bin/core_probe.exe"
LEGACY = RUNTIME / "legacy/bin"
fixture = Fixture()
records = []


def probe(*args, role="pgvn_service", ok=True, raw=False):
    result = subprocess.run([str(PROBE), *map(str, args)], cwd=EMPTY,
        env=dict(os.environ, PGVN_DSN=fixture.dsn(role)), capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
    if ok and result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    if not ok:
        assert result.returncode != 0, result.stdout
        assert b"PgVN:" in result.stderr, "Expected a handled failure, not a native crash"
    return result if raw else result.stdout.decode("utf-8").strip()


def new_repository(label="core", route=None):
    name = label + "-" + uuid.uuid4().hex[:10]
    return json.loads(probe("create", name, route or "/" + name, role="pgvn_admin"))


def publish(repository, authz="[/]\n* = rw\n", hooks=None, generation=None, groups=None):
    file = WORK / ("authz-" + uuid.uuid4().hex + ".conf")
    file.write_text(authz, encoding="utf-8")
    generation = generation or int(fixture.sql(f"SELECT generation FROM pgvn.repositories WHERE repository_id='{repository['id']}'"))
    extra = []
    if groups is not None:
        group_file = WORK / ("groups-" + uuid.uuid4().hex + ".conf")
        group_file.write_text(groups, encoding="utf-8")
        extra = [group_file]
    return probe("publish", repository["id"], generation, file, json.dumps(hooks or {}), *extra, role="pgvn_admin")


def begin(repository, user="alice"):
    return probe("begin", repository["id"], user)


def payload(data):
    file = WORK / ("payload-" + uuid.uuid4().hex + ".bin")
    file.write_bytes(data)
    return file


def paused(op, repository, user, txn, *args):
    process = subprocess.Popen([str(PROBE), op, repository["id"], user, txn, *map(str, args)],
        cwd=EMPTY, env=dict(os.environ, PGVN_DSN=fixture.dsn()), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    # Timeout for the handshake avoids blocking forever if the child fails before READY.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        line = executor.submit(process.stdout.readline).result(timeout=15)
    except Exception:
        process.kill(); process.wait(timeout=5)
        raise
    finally:
        executor.shutdown(wait=True)
    assert line.strip() == b"READY", (line, process.poll())
    return process


def finish_paused(process, success=True):
    stdout, stderr = process.communicate(b"\n", timeout=30)
    if success:
        assert process.returncode == 0, stderr
    else:
        assert process.returncode != 0 and b"PgVN:" in stderr, (stdout, stderr)
    return stdout, stderr


def creation_and_routing():
    # Guarantee that upper() changes this case-sensitive route even when the
    # random hexadecimal suffix happens to contain only digits.
    repository = new_repository("中文仓库-case")
    assert repository["id"] != repository["uuid"]
    info = json.loads(probe("info", repository["id"]))
    assert info == {"uuid": repository["uuid"], "identity": "pgvn:" + repository["id"],
                    "fs_type": "pg", "head": 0, "config_files": 0}
    prefix = "/" + repository["name"]
    nested = new_repository("nested", prefix + "/nested")
    assert json.loads(probe("resolve", prefix + "/nested/file"))["id"] == nested["id"]
    binding = json.loads(probe("resolve", prefix + "/nestedness/file"))
    assert binding["id"] == repository["id"] and binding["path"] == "/nestedness/file"
    fixture.sql(f"UPDATE pgvn.repositories SET enabled=false WHERE repository_id='{nested['id']}'")
    probe("resolve", prefix + "/nested/file", ok=False)
    # Disabled longest match must not fall through to its parent repository.
    for invalid in (prefix.upper(), prefix + "/../secret", prefix + "//x", prefix + "/./x", prefix + "/x/", prefix + "\\x"):
        probe("resolve", invalid, ok=False)
    nfc = new_repository("NFC", "/" + TEST_ID + "/caf\u00e9")
    nfd = new_repository("NFD", "/" + TEST_ID + "/cafe\u0301")
    assert json.loads(probe("resolve", "/" + TEST_ID + "/caf\u00e9"))["id"] == nfc["id"]
    assert json.loads(probe("resolve", "/" + TEST_ID + "/cafe\u0301"))["id"] == nfd["id"]
    return {"storage_id_distinct_from_uuid": True, "config_files": 0,
            "longest_segment_routing": True, "disabled_route_does_not_fall_through": True,
            "case_and_unicode_forms_distinct": True}


def atomic_creation():
    name = "race-" + uuid.uuid4().hex
    before = int(fixture.sql("SELECT count(*) FROM pgfs.repositories"))
    def create(_):
        return subprocess.run([str(PROBE), "create", name, "/" + name], cwd=EMPTY,
            env=dict(os.environ, PGVN_DSN=fixture.dsn("pgvn_admin")), capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))
    assert sum(r.returncode == 0 for r in results) == 1
    assert all(r.returncode == 0 or b"PgVN:" in r.stderr for r in results)
    after = int(fixture.sql("SELECT count(*) FROM pgfs.repositories"))
    assert after - before == 1
    assert fixture.sql(f"SELECT count(*) FROM pgvn.routes WHERE route_prefix='/{name}'") == "1"
    # The service role can open/commit but cannot create/publish control metadata.
    probe("create", "forbidden-" + name, "/forbidden-" + name, ok=False)
    assert fixture.sql("SELECT count(*) FROM pgfs.repositories") == str(after)
    return {"concurrent_creators": 8, "successful_creators": 1, "orphan_storage_rows": 0,
            "service_control_write_denied": True}


def trees_and_binary():
    repository = new_repository("binary")
    publish(repository)
    txn = begin(repository)
    probe("mkdir", repository["id"], "alice", txn, "/trunk")
    original = bytes(range(256)) * 8193 + b"\0\n\r\n\xff"
    probe("put", repository["id"], "alice", txn, "/trunk/数据.bin", payload(original))
    assert probe("commit", repository["id"], "alice", txn) == "revision=1"
    assert probe("cat", repository["id"], 1, "/trunk/数据.bin", raw=True).stdout == original
    # Persistent receipts return the same revision from another process.
    assert probe("receipt", repository["id"], "alice", txn) == "revision=1"
    probe("receipt", repository["id"], "bob", txn, ok=False)
    txn = begin(repository)
    probe("copy", repository["id"], "alice", txn, 1, "/trunk", "/branch")
    assert probe("commit", repository["id"], "alice", txn) == "revision=2"
    assert probe("cat", repository["id"], 2, "/branch/数据.bin", raw=True).stdout == original
    assert probe("verify", repository["id"]) == "3"
    before = probe("snapshot", repository["id"], 2)
    probe("pack", repository["id"])
    probe("recover", repository["id"])
    assert probe("snapshot", repository["id"], 2) == before
    return {"binary_bytes": len(original), "verified_revisions": 3, "copy_history": True,
            "commit_receipt_retry": True, "pack_recover_snapshot_unchanged": True}


def authorization_and_hooks():
    repository = new_repository("policy")
    # A new repository defaults to deny; caller-supplied author is not a privilege grant.
    txn = begin(repository)
    probe("put", repository["id"], "alice", txn, "/denied.txt", payload(b"denied"))
    probe("commit", repository["id"], "alice", txn, ok=False)
    assert json.loads(probe("info", repository["id"]))["head"] == 0
    probe("abort", repository["id"], "alice", txn)
    publish(repository, "[/]\nalice = rw\nbob = r\n")
    assert probe("policy", repository["id"], "alice", "/file", "write") == "allowed"
    assert probe("policy", repository["id"], "bob", "/file", "write") == "denied"
    assert probe("policy", repository["id"], "bob", "/file", "read") == "allowed"
    publish(repository, "[/]\n@writers = rw\nbob = r\n", groups="[groups]\nwriters = alice\n")
    assert probe("policy", repository["id"], "alice", "/file", "write") == "allowed"
    txn = begin(repository)
    probe("put", repository["id"], "alice", txn, "/allowed.txt", payload(b"value"))
    process = paused("commit-paused", repository, "alice", txn)
    try:
        publish(repository, "[/]\nalice = r\n")
        _, error = finish_paused(process, success=False)
        assert b"policy expired" in error
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
    assert json.loads(probe("info", repository["id"]))["head"] == 0
    publish(repository, hooks={"pre-commit": "reject"})
    probe("commit", repository["id"], "alice", txn, ok=False)
    assert json.loads(probe("info", repository["id"]))["head"] == 0
    publish(repository, hooks={"post-commit": "reject"})
    result = probe("commit", repository["id"], "alice", txn, ok=False, raw=True)
    assert b"revision=1" in result.stdout and b"post-commit" in result.stderr
    assert json.loads(probe("info", repository["id"]))["head"] == 1
    # Invalid authz and CAS failures cannot partially publish a generation.
    generation = fixture.sql(f"SELECT generation FROM pgvn.repositories WHERE repository_id='{repository['id']}'")
    bad = WORK / "bad-authz.conf"
    bad.write_text("[broken\n", encoding="utf-8")
    probe("publish", repository["id"], generation, bad, "{}", role="pgvn_admin", ok=False)
    good = WORK / "good-authz.conf"
    good.write_text("[/]\n* = rw\n", encoding="utf-8")
    probe("publish", repository["id"], 1, good, "{}", role="pgvn_admin", ok=False)
    assert fixture.sql(f"SELECT generation FROM pgvn.repositories WHERE repository_id='{repository['id']}'") == generation
    return {"default_deny": True, "username_path_access": True, "stale_inflight_commit_rejected": True,
            "pre_hook_blocks_publication": True, "post_hook_failure_preserves_revision": True,
            "invalid_authz_and_generation_conflict_atomic": True}


def writer_leases_and_metrics():
    repository = new_repository("leases")
    publish(repository)
    txn = begin(repository)
    data = payload(b"writer-data\0\xff")
    writer = paused("hold-writer", repository, "alice", txn, "/file.bin", data)
    try:
        probe("put", repository["id"], "alice", txn, "/file.bin", data, ok=False)
        probe("commit", repository["id"], "alice", txn, ok=False)
        finish_paused(writer)
    finally:
        if writer.poll() is None:
            writer.kill(); writer.wait(timeout=5)
    assert probe("commit", repository["id"], "alice", txn) == "revision=1"
    txn = begin(repository)
    writer = paused("hold-writer", repository, "alice", txn, "/file.bin", data)
    writer.kill(); writer.wait(timeout=5)
    # A process exit releases its session lock; a fresh process can finish the stream.
    probe("put", repository["id"], "alice", txn, "/file.bin", data)
    assert probe("commit", repository["id"], "alice", txn) == "revision=2"
    metrics = json.loads(probe("metrics", repository["id"]))
    assert metrics == {"sql_delta": 20, "errors": 0, "open": 2, "closed": 1, "snapshot_sql": 0, "ready": False}
    capacity = json.loads(probe("capacity", repository["id"]))
    assert capacity == {"opened_fs": 31, "connections_at_limit": 32, "connections_after_close": 1}
    assert fixture.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn'") == "0"
    return {"cross_process_writer_exclusion": True, "open_stream_commit_rejected": True,
            "disconnect_releases_session_lock": True, "metrics": metrics, "connection_capacity": capacity}


def legacy_adapter_regression():
    sys.path.insert(0, str(ROOT / "tests"))
    modules = [importlib.import_module(name) for name in ("test_p0", "test_p1", "test_p2")]
    for module in modules:
        module.ROOT = WORK / "legacy-fixture"
        module.BIN = LEGACY
        if hasattr(module, "PG"):
            module.PG = fixture.pg
        if hasattr(module, "DSN"):
            module.DSN = fixture.dsn("pgvn_admin")
    def legacy_native(program, *args, check=True):
        result = subprocess.run([str(LEGACY / program), *map(str, args)],
            env=dict(os.environ, PGFS_DSN=fixture.dsn("pgvn_admin")), capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        if check and result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace"))
        return result
    modules[0].native = legacy_native
    modules[0].sql = lambda statement: fixture.sql(statement).encode("utf-8")
    suite = unittest.TestSuite()
    excluded = []
    for module, cls in zip(modules, ("NativeLoaderTests", "NativeTreeTests", "CompatibilityTests")):
        for test in unittest.defaultTestLoader.loadTestsFromTestCase(getattr(module, cls)):
            if test._testMethodName == "test_06_database_restart_preserves_revision_and_binary_properties":
                excluded.append({"test": test.id(), "reason": "Original fixture hardcodes port15432/data path; equivalent PgVN restart gate below uses verified16432 directory."})
            else:
                suite.addTest(test)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (WORK / "legacy-regression.log").write_text(log.getvalue(), encoding="utf-8")
    assert result.wasSuccessful(), log.getvalue()
    return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "excluded": excluded, "assertions": "Original test assertions unchanged; fixture paths/DSN adapted."}


def restart_and_pathlessness():
    repository = new_repository("restart")
    publish(repository)
    txn = begin(repository)
    data = b"durable\0\xff\r\n"
    probe("put", repository["id"], "alice", txn, "/durable.bin", payload(data))
    probe("commit", repository["id"], "alice", txn)
    fixture.stop()
    fixture.initialize()
    assert probe("cat", repository["id"], 1, "/durable.bin", raw=True).stdout == data
    assert probe("verify", repository["id"]) == "2"
    assert fixture.sql("SHOW fsync") == "on" and fixture.sql("SHOW full_page_writes") == "on"
    assert list(EMPTY.iterdir()) == []
    return {"database_restart_preserves_binary_history": True, "fsync": "on", "full_page_writes": "on",
            "pathless_working_directory_empty": True}


def main():
    EMPTY.mkdir(parents=True)
    fixture_report = fixture.initialize()
    checks = (creation_and_routing, atomic_creation, trees_and_binary, authorization_and_hooks,
              writer_leases_and_metrics, legacy_adapter_regression, restart_and_pathlessness)
    for check in checks:
        start = time.monotonic()
        try:
            record = {"name": check.__name__, "status": "PASS", "details": check()}
        except Exception:
            record = {"name": check.__name__, "status": "FAIL", "error": traceback.format_exc()}
        record["elapsed_seconds"] = time.monotonic() - start
        records.append(record)
        print(json.dumps(record, ensure_ascii=True), flush=True)
        if record["status"] == "FAIL":
            break
    passed = len(records) == len(checks) and all(r["status"] == "PASS" for r in records)
    report = {"test_id": TEST_ID, "milestone": "M1", "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "status": "PASS" if passed else "FAIL", "fixture": fixture_report, "records": records,
              "core_probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
              "legacy_module_sha256": hashlib.sha256((LEGACY / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    folder = ROOT / "artifacts/pgvn/tests"
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2) + "\n"
    (folder / (TEST_ID + ".json")).write_text(text, encoding="utf-8")
    (folder / "m1-latest.json").write_text(text, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
