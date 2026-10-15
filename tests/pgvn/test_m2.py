"""M2 native admin gate. All databases belong to the verified loopback fixture."""
from pathlib import Path
import base64
import concurrent.futures
import ctypes
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from fixture import Fixture, ROOT, RUNTIME

TEST_ID = "m2-" + uuid.uuid4().hex[:12]
DATABASE = "pgvn_" + TEST_ID.replace("-", "_")
WORK = RUNTIME / "tests" / TEST_ID
EMPTY = WORK / "pathless-working-directory"
ADMIN = RUNTIME / "bin/pgvnadmin.exe"
PROBE = RUNTIME / "bin/core_probe.exe"
LEGACY = RUNTIME / "legacy/bin"
fixture = Fixture()
state = {}
records = []


def dsn(role="pgvn_admin", database=DATABASE):
    return f"host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable"


def sql(statement, database=DATABASE, role="pgvn_owner"):
    return fixture.sql(statement, database=database, role=role)


def cli(*args, role="pgvn_admin", database=DATABASE, expected=0, input=None, executable=ADMIN, environment=None):
    env = dict(os.environ, PGVN_DSN=dsn(role, database))
    if environment:
        env.update(environment)
    result = subprocess.run([str(executable), *map(str, args)], cwd=EMPTY, env=env, input=input,
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    assert result.returncode == expected, (args, result.returncode, result.stdout[:1500], result.stderr[:2500])
    if expected and "--json" in args:
        report = json.loads(result.stderr)
        assert report["ok"] is False and report["error"]["exit_code"] == expected
    return result


def meta(*args, **kwargs):
    result = cli(*args, "--json", **kwargs)
    return json.loads(result.stdout if result.returncode == 0 else result.stderr)


def native(tool, *args, input=None, pg_dsn=None):
    extra = ["--non-interactive", "--config-dir", str(WORK / "svn-config")] if tool in ("svn", "svnmucc") else []
    result = subprocess.run([str(LEGACY / (tool + ".exe")), *map(str, args), *extra], cwd=WORK,
        env=dict(os.environ, PGFS_DSN=pg_dsn or dsn()), input=input, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    assert result.returncode == 0, (tool, args, result.stderr[:2500])
    return result.stdout


def probe(*args, expected=0, role="pgvn_service"):
    result = subprocess.run([str(PROBE), *map(str, args)], cwd=EMPTY,
        env=dict(os.environ, PGVN_DSN=dsn(role)), capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert result.returncode == expected, (args, result.returncode, result.stderr)
    return result.stdout.strip()


def file(name, data):
    path = WORK / name
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def new_repo(name, **kwargs):
    return meta("repo", "create", name, **kwargs)


def allow_writes(repository):
    current = meta("config", "show", repository)
    rules = file("allow-" + uuid.uuid4().hex + ".conf", "[/]\n* = rw\n")
    return meta("config", "publish", repository, "--expected-generation", current["generation"], "--authz", rules)


def wait_file(path, process, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, "Expected child marker was not created"
        time.sleep(0.05)


def migration_and_privileges():
    fixture.assert_ours()
    migration_role = "pgvn_migrate_" + TEST_ID.split("-", 1)[1]
    sql(f"CREATE ROLE {migration_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT", database="postgres")
    sql(f"CREATE DATABASE {DATABASE} OWNER {migration_role} ENCODING 'UTF8' TEMPLATE template0", database="postgres")
    meta("db", "check", role=migration_role, expected=4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: meta("db", "init", role=migration_role), range(4)))
    assert all(r["ok"] for r in results)
    check = meta("db", "check", role="pgvn_owner")
    assert check["schema"] == {"pgfs": 4, "pgvn": 5, "checksums": "match"}
    installed = sql("SELECT json_agg(v ORDER BY version)::text FROM pgvn.schema_versions v")
    meta("db", "migrate", role=migration_role)
    assert sql("SELECT json_agg(v ORDER BY version)::text FROM pgvn.schema_versions v") == installed
    original = sql("SELECT migration_sha256 FROM pgvn.schema_versions WHERE version=2")
    try:
        sql("UPDATE pgvn.schema_versions SET migration_sha256=repeat('0',64) WHERE version=2")
        meta("db", "check", role="pgvn_owner", expected=4)
        meta("db", "migrate", role="pgvn_owner", expected=4)
    finally:
        sql(f"UPDATE pgvn.schema_versions SET migration_sha256='{original}' WHERE version=2")
    try:
        sql("INSERT INTO pgfs.schema_versions(version) VALUES(99)")
        meta("db", "check", role="pgvn_owner", expected=4)
        meta("db", "migrate", role="pgvn_owner", expected=4)
        assert sql("SELECT count(*) FROM pgvn.schema_versions") == "5"
    finally:
        sql("DELETE FROM pgfs.schema_versions WHERE version=99")
    meta("db", "grant", "--admin-role", "pgvn_admin", "--service-role", "pgvn_service", "--monitor-role", "pgvn_monitor", role=migration_role)
    assert sql(f"SELECT rolsuper FROM pg_roles WHERE rolname='{migration_role}'") == "f"
    assert meta("diagnose")["privileges"]["admin"] is True
    assert meta("diagnose", role="pgvn_service")["privileges"]["admin"] is False
    meta("repo", "create", "forbidden", role="pgvn_service", expected=5)
    assert sql("SELECT count(*) FROM pgfs.repositories") == "0"
    return {"database": DATABASE, "concurrent_initializers": 4, "idempotent_migrate": True,
            "checksum_tampering_rejected": True, "future_schema_refused_before_migration": True,
            "schema": check["schema"], "migration_owner_superuser": False, "service_admin_denied": True}


def cli_contract_and_catalog():
    public_uuid = str(uuid.uuid4())
    name = "中文 ' 仓库 \"A\""
    repository = meta("repo", "create", name, "--route", "/测试/A", "--uuid", public_uuid)
    assert repository["uuid"] == public_uuid and repository["id"] != public_uuid
    assert meta("repo", "show", "name:" + name)["id"] == repository["id"]
    assert meta("repo", "show", "id:" + repository["id"])["id"] == repository["id"]
    assert len(meta("repo", "list", "--limit", 1)["items"]) == 1
    assert meta("repo", "list", "--offset", 1)["items"] == []
    meta("repo", "create", name, expected=7)
    meta("repo", "show", "absent-" + TEST_ID, expected=6)
    for args in (("repo", "create"), ("repo", "show", "id:bad"), ("repo", "list", "--limit", 0),
                 ("repo", "create", "bad", "--route", "/../bad"), ("dump", repository["id"]),
                 ("repo", "list", "--scope", "write")):
        meta(*args, expected=2)
    config = file("连接 configuration.ini", "[pgvn]\nconnection = " + dsn() + "\n")
    meta("db", "check", "--config", config, environment={"PGVN_DSN": "host=127.0.0.1 port=1 password=SENTINEL_SECRET"})
    failed = cli("diagnose", "--json", expected=3, environment={"PGVN_DSN": "host=127.0.0.1 port=1 connect_timeout=1 password=SENTINEL_SECRET"})
    assert b"SENTINEL_SECRET" not in failed.stdout + failed.stderr
    generation = repository["generation"]
    disabled = meta("repo", "disable", repository["id"])
    assert disabled["enabled"] is False and disabled["generation"] == generation + 1
    assert meta("repo", "disable", repository["id"])["generation"] == disabled["generation"]
    assert cli("look", "uuid", repository["id"]).stdout.strip().decode() == public_uuid
    assert meta("repo", "enable", repository["id"])["generation"] == generation + 2
    return {"unicode_names_and_config": True, "selectors_and_pagination": True,
            "exit_classes_verified": [2, 3, 4, 5, 6, 7], "dsn_redaction": True, "disable_invalidates_generation": True}


def seed_fsfs():
    source = WORK / "fsfs-source"
    native("svnadmin", "create", source)
    url = source.as_uri()
    files = WORK / "import"
    (files / "trunk/sub").mkdir(parents=True)
    content = bytes(range(256)) * 4097 + b"\0\r\n\xff"
    (files / "trunk/data.bin").write_bytes(content)
    (files / "trunk/empty.bin").write_bytes(b"")
    (files / "trunk/中文.txt").write_text("初始内容\n", encoding="utf-8")
    (files / "trunk/sub/nested.txt").write_bytes(b"nested\n")
    native("svn", "import", files, url, "-m", "initial", "--username", "alice")
    native("svn", "copy", url + "/trunk", url + "/branch", "-m", "branch", "--username", "alice")
    wc = WORK / "working-copy"
    native("svn", "checkout", url + "/trunk", wc)
    (wc / "data.bin").write_bytes(b"prefix\0" + content[100:] + b"\xfftail")
    native("svn", "propset", "custom:binary", "-F", file("property.bin", b"a\0b\xff"), wc / "data.bin")
    native("svn", "propset", "custom:empty", "", wc / "data.bin")
    native("svn", "commit", wc, "-m", "binary and properties", "--username", "alice")
    native("svn", "move", url + "/branch/sub/nested.txt", url + "/branch/moved.txt", "-m", "move", "--username", "alice")
    state["source"] = source
    state["source_dump"] = native("svnadmin", "dump", source, "--quiet")
    return source


def history_roundtrip():
    source = seed_fsfs()
    repository = new_repo("loaded-" + TEST_ID)
    meta("repo", "disable", repository["id"])
    assert meta("load", repository["id"], input=state["source_dump"])["head"] == 4
    assert cli("look", "uuid", repository["id"]).stdout.strip() == native("svnlook", "uuid", source).strip()
    assert meta("verify", repository["id"])["verified_revisions"] == 5
    assert meta("verify", repository["id"], "-r", "1:3")["verified_revisions"] == 3
    meta("verify", repository["id"], "-r", "4:1", expected=2)
    meta("repo", "enable", repository["id"])
    for revision in range(5):
        expected_snapshot = native("fs_probe", "snapshot", source, revision).strip().splitlines()
        assert probe("snapshot", repository["id"], revision).splitlines() == expected_snapshot
        # Headless Windows svnlook uses the ANSI code page and renders Chinese
        # names as '?'. Use the lossless, hex-encoded public FS API oracle.
        expected_nodes = []
        for line in expected_snapshot:
            fields = line.decode("ascii").split("|")
            if fields[0] == "N":
                expected_nodes.append({"path": bytes.fromhex(fields[1]).decode("utf-8"), "kind": "dir" if fields[2] == "2" else "file"})
        assert meta("look", "tree", repository["id"], "-r", revision)["nodes"] == expected_nodes
        expected_text = ["/" if n["path"] == "/" else n["path"][1:] + ("/" if n["kind"] == "dir" else "") for n in expected_nodes]
        assert cli("look", "tree", repository["id"], "--full-paths", "-r", revision).stdout.decode("utf-8").splitlines() == expected_text
        xml = ET.fromstring(native("svn", "log", source.as_uri(), "--xml", "--verbose", "-r", revision))
        expected = {p.text: p.attrib for p in xml.findall("./logentry/paths/path")}
        changes = meta("look", "changed", repository["id"], "-r", revision)["changes"]
        assert {p["path"] for p in changes} == set(expected)
        for change in changes:
            attributes = expected[change["path"]]
            assert change["action"] == {"A": "add", "D": "delete", "M": "modify", "R": "replace"}[attributes["action"]]
            assert change["copyfrom_path"] == attributes.get("copyfrom-path")
    data = native("svnlook", "cat", source, "trunk/data.bin", "-r", 3)
    assert cli("look", "cat", repository["id"], "trunk/data.bin", "-r", 3).stdout == data
    assert base64.b64decode(meta("look", "cat", repository["id"], "trunk/data.bin", "-r", 3)["data"]) == data
    assert cli("look", "propget", repository["id"], "custom:binary", "trunk/data.bin", "-r", 3).stdout == b"a\0b\xff"
    assert cli("look", "propget", repository["id"], "custom:empty", "trunk/data.bin", "-r", 3).stdout == b""
    props = meta("look", "proplist", repository["id"], "trunk/data.bin", "-r", 3, "--verbose")["properties"]
    assert {p["name"]: base64.b64decode(p["value"]) for p in props}["custom:binary"] == b"a\0b\xff"
    assert cli("look", "propget", repository["id"], "svn:log", "--revprop", "-r", 3).stdout == native("svnlook", "propget", "--revprop", "-r", 3, source, "svn:log")
    info = meta("look", "info", repository["id"], "-r", 3)
    for field, prop in (("author", "svn:author"), ("date", "svn:date"), ("log", "svn:log")):
        assert base64.b64decode(info[field]) == native("svnlook", "propget", "--revprop", "-r", 3, source, prop)
    dump = cli("dump", repository["id"], "--deltas").stdout
    back = WORK / "fsfs-return"
    native("svnadmin", "create", back)
    native("svnadmin", "load", back, "--quiet", input=dump)
    for revision in range(5):
        assert native("fs_probe", "snapshot", back, revision) == native("fs_probe", "snapshot", source, revision)
    saved = WORK / "saved.dump"
    cli("dump", repository["id"], "--file", saved, "--deltas")
    original_file = saved.read_bytes()
    cli("dump", repository["id"], "--file", saved, expected=7)
    assert saved.read_bytes() == original_file
    ignored = new_repo("ignored-uuid-" + TEST_ID)
    meta("repo", "disable", ignored["id"])
    assert meta("load", ignored["id"], "--file", saved, "--ignore-uuid")["head"] == 4
    assert meta("look", "uuid", ignored["id"])["uuid"] == ignored["uuid"]
    zero = native("svnadmin", "dump", source, "--quiet", "-r", "0:0")
    assert meta("load", ignored["id"], "--force-uuid", input=zero)["head"] == 4
    assert meta("look", "uuid", ignored["id"])["uuid"] == meta("look", "uuid", repository["id"])["uuid"]
    hooked = new_repo("load-hooks-" + TEST_ID)
    generation = meta("repo", "disable", hooked["id"])["generation"]
    policy = file("load-hook-authz.conf", "[/]\n* = rw\n")
    hooks = file("load-hook-reject.json", '{"pre-commit":"reject"}')
    meta("config", "publish", hooked["id"], "--authz", policy, "--hooks", hooks, "--expected-generation", generation)
    meta("load", hooked["id"], "--use-hooks", input=state["source_dump"], expected=1)
    assert meta("look", "youngest", hooked["id"])["youngest"] == 0
    for txn in meta("look", "transactions", hooked["id"])["transactions"]:
        meta("maintenance", "abort-txn", hooked["id"], txn)
    assert meta("load", hooked["id"], input=state["source_dump"])["head"] == 4
    state["repository"] = repository
    return {"revisions_compared": 5, "snapshot_tree_changed_copy_history_match_fsfs": True,
            "binary_bytes": len(data), "binary_empty_properties": True, "standard_dump_roundtrip": True,
            "file_and_stdio_streams": True, "existing_dump_file_not_overwritten": True,
            "force_and_ignore_uuid": True, "optional_load_hooks": True}


def incremental_and_registration():
    source = state["source"]
    repository = new_repo("incremental-" + TEST_ID)
    meta("repo", "disable", repository["id"])
    first = native("svnadmin", "dump", source, "--quiet", "-r", "0:2")
    later = native("svnadmin", "dump", source, "--quiet", "-r", "3:4", "--incremental", "--deltas")
    assert meta("load", repository["id"], "-r", "0:2", input=state["source_dump"])["head"] == 2
    assert meta("load", repository["id"], "-r", "3:4", input=state["source_dump"])["head"] == 4
    exported = cli("dump", repository["id"], "-r", "3:4", "--incremental", "--deltas").stdout
    fsfs = WORK / "fsfs-incremental-return"
    native("svnadmin", "create", fsfs)
    native("svnadmin", "load", fsfs, "--quiet", input=first)
    native("svnadmin", "load", fsfs, "--quiet", input=exported)
    assert native("fs_probe", "snapshot", fsfs, 4) == native("fs_probe", "snapshot", source, 4)
    legacy = WORK / "legacy-pg-source"
    native("svnadmin", "create", legacy, "--fs-type", "pg")
    import configparser
    binding = configparser.ConfigParser()
    binding.read(legacy / "db/pgfs.conf", encoding="utf-8")
    original_id = binding["pgfs"]["repository"]
    native("svnadmin", "load", legacy, "--quiet", input=state["source_dump"])
    public_uuid = native("svnlook", "uuid", legacy).decode().strip()
    registered = meta("repo", "register-existing", "registered-" + TEST_ID, original_id)
    assert registered["id"] == original_id and registered["uuid"] == public_uuid and original_id != public_uuid
    assert registered["enabled"] is False
    assert meta("verify", original_id)["verified_revisions"] == 5
    assert cli("look", "cat", original_id, "trunk/data.bin", "-r", 3).stdout == native("svnlook", "cat", source, "trunk/data.bin", "-r", 3)
    return {"incremental_import_export": True, "existing_storage_id_preserved": True,
            "existing_public_uuid_preserved": True, "registered_history_verified": 5}


def policies_and_tokens():
    repository = state["repository"]["id"]
    current = meta("config", "show", repository)
    rules = file("roles.conf", "[/]\n@writers = rw\nbob = r\n")
    groups = file("groups.conf", "[groups]\nwriters = alice\n")
    hooks = file("hooks.json", '{"pre-revprop-change":"allow"}')
    assert meta("authz", "validate", "--authz", rules, "--groups", groups)["ok"]
    updated = meta("config", "publish", repository, "--authz", rules, "--groups", groups, "--hooks", hooks,
                   "--expected-generation", current["generation"])
    assert updated["generation"] == current["generation"] + 1
    preserved = meta("config", "publish", repository, "--authz", rules, "--expected-generation", updated["generation"])
    assert preserved["groups"] == updated["groups"] and preserved["hooks"] == updated["hooks"]
    assert meta("authz", "check", repository, "alice", "/trunk", "write")["allowed"] is True
    assert meta("authz", "check", repository, "bob", "/trunk", "write")["allowed"] is False
    meta("config", "publish", repository, "--authz", rules, "--groups", groups, "--expected-generation", current["generation"], expected=7)
    bad = file("invalid-authz.conf", "[broken\n")
    meta("authz", "validate", "--authz", bad, expected=2)
    token = meta("monitor-token", "create", "--label", "测试只读令牌", "--expires-in", 3600)
    assert token["scope"] == "monitor:read" and len(token["token"]) == 69
    stored = sql(f"SELECT encode(token_hash,'hex') FROM pgvn.monitor_tokens WHERE token_id='{token['id']}'")
    assert stored == hashlib.sha256(token["token"].encode()).hexdigest()
    listing = cli("monitor-token", "list", "--json").stdout
    assert token["token"].encode() not in listing and stored.encode() not in listing
    meta("monitor-token", "expire", token["id"], "--expires-in", 0)
    items = meta("monitor-token", "list")["items"]
    assert next(t for t in items if t["id"] == token["id"])["expired"] is True
    first_revoke = meta("monitor-token", "revoke", token["id"])
    assert meta("monitor-token", "revoke", token["id"])["revoked_at"] == first_revoke["revoked_at"]
    meta("monitor-token", "create", "--label", "forbidden", role="pgvn_service", expected=5)
    meta("monitor-token", "create", "--label", "forbidden", "--scope", "write", expected=2)
    return {"versioned_authz_groups": True, "stale_generation_rejected": True, "token_entropy_bits": 256,
            "token_sha256_only_in_database": True, "token_expiry_and_revoke": True, "no_token_in_list_or_evidence": True}


def hotcopy_and_maintenance():
    repository = state["repository"]["id"]
    backup = meta("maintenance", "hotcopy", repository, "backup-" + TEST_ID)
    assert backup["id"] != repository and backup["uuid"] == cli("look", "uuid", repository).stdout.decode().strip()
    assert meta("repo", "show", backup["id"])["enabled"] is False
    assert meta("verify", backup["id"])["verified_revisions"] == 5
    allow_writes(repository)
    txn = probe("begin", repository, "alice").decode()
    probe("put", repository, "alice", txn, "/tail.bin", file("tail.bin", b"tail\0\xff"))
    assert probe("commit", repository, "alice", txn) == b"revision=5"
    meta("maintenance", "hotcopy", repository, backup["id"], "--incremental")
    assert meta("look", "youngest", backup["id"])["youngest"] == 5
    assert cli("look", "cat", backup["id"], "tail.bin").stdout == b"tail\0\xff"
    meta("repo", "enable", backup["id"])
    meta("maintenance", "hotcopy", repository, backup["id"], "--incremental", expected=2)
    allow_writes(backup["id"])
    txn = probe("begin", backup["id"], "alice").decode()
    probe("put", backup["id"], "alice", txn, "/diverged.txt", file("diverged.txt", b"diverged"))
    probe("commit", backup["id"], "alice", txn)
    meta("repo", "disable", backup["id"])
    cli("maintenance", "hotcopy", repository, backup["id"], "--incremental", "--json", expected=7)
    assert meta("look", "youngest", backup["id"])["youngest"] == 6
    before = sql("SELECT count(*) FROM pgfs.repositories")
    meta("maintenance", "hotcopy", repository, "collision-" + TEST_ID, "--route", state["repository"]["routes"][0], expected=7)
    assert sql("SELECT count(*) FROM pgfs.repositories") == before
    snapshot = probe("snapshot", repository, 5)
    meta("maintenance", "pack", repository)
    meta("maintenance", "recover", repository)
    assert probe("snapshot", repository, 5) == snapshot
    return {"full_and_incremental_hotcopy": True, "independent_id_preserved_uuid": True,
            "divergence_rejected_without_changes": True, "route_conflict_rolls_back_storage": True,
            "pack_recover_preserve_snapshot": True}


def freeze_and_lifecycle():
    repository = state["repository"]["id"]
    # Stage before taking the freeze: FK checks during begin/write may also
    # wait for the repository row lock. The gate specifically measures publish.
    txn = probe("begin", repository, "alice").decode()
    probe("put", repository, "alice", txn, "/frozen.txt", file("frozen.txt", b"after freeze"))
    ready, release = WORK / "freeze-ready", WORK / "freeze-release"
    child = file("freeze-child.py", "from pathlib import Path\nimport sys,time,json\n"
                 "Path(sys.argv[1]).write_text('ready')\n"
                 "while not Path(sys.argv[2]).exists(): time.sleep(.02)\n"
                 "print(json.dumps(sys.argv[3:]))\n")
    arguments = ["spaces and \"quotes\"", "trailing\\", "中文"]
    process = subprocess.Popen([str(ADMIN), "maintenance", "freeze", repository, "--timeout", "15", "--",
        sys.executable, str(child), str(ready), str(release), *arguments], cwd=EMPTY,
        env=dict(os.environ, PGVN_DSN=dsn()), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    commit = None
    try:
        wait_file(ready, process)
        commit = subprocess.Popen([str(PROBE), "commit", repository, "alice", txn], cwd=EMPTY,
            env=dict(os.environ, PGVN_DSN=dsn("pgvn_service")), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
        assert commit.poll() is None, "Commit must wait for freeze row lock"
        release.write_text("release")
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, error
        assert json.loads(output) == arguments
        output, error = commit.communicate(timeout=15)
        assert commit.returncode == 0 and b"revision=6" in output, error
    finally:
        release.write_text("release")
        for child_process in (process, commit):
            if child_process and child_process.poll() is None:
                child_process.kill(); child_process.wait(timeout=5)
    timeout_child = file("timeout-child.py", "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(p.pid))\n"
        "time.sleep(60)\n")
    pid_file = WORK / "grandchild.pid"
    cli("maintenance", "freeze", repository, "--timeout", 1, "--", sys.executable, timeout_child, pid_file, expected=1)
    assert pid_file.exists()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
    if handle:
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        assert kernel.WaitForSingleObject(handle, 2000) == 0
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
    disposable = new_repo("purge-" + TEST_ID)
    rid = disposable["id"]
    meta("maintenance", "purge", rid, "--confirm-id", rid, expected=2)
    allow_writes(rid)
    txn = probe("begin", rid, "alice").decode()
    pending = b"unpublished\0\xff"
    probe("put", rid, "alice", txn, "/pending.bin", file("pending.bin", pending))
    assert txn in meta("look", "transactions", rid)["transactions"]
    assert base64.b64decode(meta("look", "txn-info", rid, "--txn", txn)["author"]) == b"alice"
    assert cli("look", "cat", rid, "/pending.bin", "--txn", txn).stdout == pending
    assert meta("look", "changed", rid, "--txn", txn)["changes"][0]["path"] == "/pending.bin"
    assert "svn:author" in {p["name"] for p in meta("look", "proplist", rid, "--revprop", "--txn", txn)["properties"]}
    meta("repo", "disable", rid)
    meta("maintenance", "purge", rid, "--confirm-id", rid, expected=7)
    meta("maintenance", "abort-txn", rid, txn)
    assert txn not in meta("look", "transactions", rid)["transactions"]
    meta("look", "txn-info", rid, "--txn", txn, expected=6)
    meta("repo", "enable", rid)
    held = subprocess.Popen([str(PROBE), "hold-open", rid], cwd=EMPTY, env=dict(os.environ, PGVN_DSN=dsn("pgvn_service")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(held.stdout.readline).result(timeout=15).strip() == b"READY"
        meta("repo", "disable", rid)
        meta("maintenance", "purge", rid, "--confirm-id", rid, expected=7)
    finally:
        if held.poll() is None:
            held.communicate(b"\n", timeout=10)
    meta("maintenance", "purge", rid, "--confirm-id", str(uuid.uuid4()), expected=2)
    meta("maintenance", "purge", rid, "--confirm-id", rid)
    meta("repo", "show", rid, expected=6)
    assert sql(f"SELECT count(*) FROM pgfs.repositories WHERE repository_id='{rid}'") == "0"
    assert list(EMPTY.iterdir()) == []
    return {"freeze_blocks_commit_until_child_exits": True, "child_argument_quoting": True,
            "timeout_kills_child_tree": True, "purge_requires_disable_and_drained_handles": True,
            "purge_clears_catalog_and_storage": True, "pathless_working_directory_empty": True}


def portable_binary():
    portable = WORK / "便携 native/bin"
    portable.mkdir(parents=True)
    shutil.copy2(ADMIN, portable / ADMIN.name)
    for dll in (RUNTIME / "bin").glob("*.dll"):
        shutil.copy2(dll, portable / dll.name)
    assert meta("db", "check", executable=portable / ADMIN.name)["schema"]["pgvn"] == 5
    assert meta("look", "youngest", state["repository"]["id"], executable=portable / ADMIN.name)["youngest"] == 6
    return {"standalone_binary_and_dll_directory": True, "runtime_python_node_not_required": True}


def main():
    EMPTY.mkdir(parents=True)
    fixture.initialize()
    checks = (migration_and_privileges, cli_contract_and_catalog, history_roundtrip,
              incremental_and_registration, policies_and_tokens, hotcopy_and_maintenance,
              freeze_and_lifecycle, portable_binary)
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
    report = {"test_id": TEST_ID, "milestone": "M2", "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "status": "PASS" if passed else "FAIL", "database": DATABASE, "work_directory": str(WORK),
              "pgvnadmin_sha256": hashlib.sha256(ADMIN.read_bytes()).hexdigest(), "records": records,
              "scope": "Native admin/core gates; transport protocols and full monitor UI are subsequent milestones."}
    destination = ROOT / "artifacts/pgvn/tests"
    destination.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, indent=2) + "\n"
    (destination / (TEST_ID + ".json")).write_text(data, encoding="utf-8")
    (destination / "m2-latest.json").write_text(data, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
