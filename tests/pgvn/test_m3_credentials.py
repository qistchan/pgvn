"""M3 credential component checks. This is not the complete M3 release gate."""
from pathlib import Path
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import traceback
import uuid
from fixture import Fixture, ROOT, RUNTIME

TEST_ID = "m3-credentials-" + uuid.uuid4().hex[:12]
DATABASE = "pgvn_m3c_" + TEST_ID.rsplit("-", 1)[1]
OWNER = "pgvn_migrate_" + TEST_ID.rsplit("-", 1)[1]
WORK = RUNTIME / "tests" / TEST_ID
EMPTY = WORK / "pathless-working-directory"
ADMIN = RUNTIME / "bin/pgvnadmin.exe"
PROBE = RUNTIME / "bin/core_probe.exe"
fixture = Fixture()


def dsn(role):
    return f"host=127.0.0.1 port=16432 dbname={DATABASE} user={role} sslmode=disable"


def sql(statement):
    return fixture.sql(statement, database=DATABASE)


def run(executable, args, expected=0, role="pgvn_admin", input=None):
    result = subprocess.run([str(executable), *map(str, args)], cwd=EMPTY,
        env=dict(os.environ, PGVN_DSN=dsn(role)), input=input, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert result.returncode in (expected if isinstance(expected, tuple) else (expected,)), (args, result.returncode, result.stdout[:1000], result.stderr[:1500])
    return result


def admin(*args, **kwargs):
    result = run(ADMIN, [*args, "--json"], **kwargs)
    return json.loads(result.stdout if result.returncode == 0 else result.stderr)


def digest(key, user="测试用户", expected=0, previous=None):
    arguments=["credential-digest", key, "pgvn-test", user]
    if previous: arguments.append(previous)
    result = run(PROBE, arguments, role="pgvn_service", expected=expected)
    return json.loads(result.stdout) if expected == 0 else None


def setup_database():
    EMPTY.mkdir(parents=True)
    fixture.initialize()
    fixture.assert_ours()
    fixture.sql(f"CREATE ROLE {OWNER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT", database="postgres")
    fixture.sql(f"CREATE DATABASE {DATABASE} OWNER {OWNER} ENCODING 'UTF8' TEMPLATE template0", database="postgres")
    admin("db", "init", role=OWNER)
    admin("db", "grant", "--admin-role", "pgvn_admin", "--service-role", "pgvn_service", "--monitor-role", "pgvn_monitor", role=OWNER)
    assert admin("db", "check")["schema"]["pgvn"] == 5


def checks():
    setup_database()
    key = WORK / "部署 credential-v1.key"
    other = WORK / "other.key"
    assert admin("key", "create", key, "test-v1")["ok"]
    admin("key", "create", other, "test-v1")
    original = hashlib.sha256(key.read_bytes()).digest()
    admin("key", "create", key, "test-v1", expected=7)
    assert hashlib.sha256(key.read_bytes()).digest() == original
    admin("key", "create", WORK / "bad.key", "invalid/key", expected=2)
    password = ("随机/密碼 " + secrets.token_urlsafe(32)).encode("utf-8")
    admin("user", "set", "pgvn-test", "测试用户", "--key-file", key, input=password + b"\r\n")
    assert digest(key) == {"generation": 1, "sha1": hashlib.sha1(password).hexdigest()}
    digest(other, expected=1)
    digest(key, "missing-user", expected=1)
    metadata = admin("user", "list", "pgvn-test")
    assert len(metadata) == 1 and metadata[0]["key_id"] == "test-v1"
    assert all(k not in metadata[0] for k in ("ciphertext", "nonce", "authentication_tag", "password"))
    stored = sql("SELECT encode(ciphertext,'hex') FROM pgvn.credentials")
    assert password.hex() not in stored and stored != password.hex()
    before = sql("SELECT generation FROM pgvn.credentials")
    for invalid in (b"", b"nul\0secret", b"x" * 4097, b"\xff"):
        admin("user", "set", "pgvn-test", "测试用户", "--key-file", key, input=invalid, expected=2)
    assert sql("SELECT generation FROM pgvn.credentials") == before
    admin("user", "set", "pgvn-test", "测试用户", "--key-file", key, input=password, role="pgvn_service", expected=5)
    assert sql("SELECT has_table_privilege('pgvn_monitor','pgvn.credentials','SELECT')") == "f"
    assert sql("SELECT has_table_privilege('pgvn_service','pgvn.credentials','UPDATE')") == "f"
    tag = sql("SELECT encode(authentication_tag,'hex') FROM pgvn.credentials")
    try:
        sql("UPDATE pgvn.credentials SET authentication_tag=set_byte(authentication_tag,0,get_byte(authentication_tag,0)#1)")
        digest(key, expected=1)
    finally:
        sql(f"UPDATE pgvn.credentials SET authentication_tag=decode('{tag}','hex')")
    # A copied ciphertext must not authenticate a different identity.
    sql("INSERT INTO pgvn.credentials SELECT identity_domain,'copied-user',generation,enabled,key_id,nonce,ciphertext,authentication_tag,updated_at FROM pgvn.credentials")
    digest(key, "copied-user", expected=1)
    # A protected ACL containing an Everyone allow entry must also be rejected.
    bad_acl = WORK / "broad-access.key"
    admin("key", "create", bad_acl, "test-broad")
    acl = subprocess.run(["icacls.exe", str(bad_acl), "/grant", "*S-1-1-0:(R)"], capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    assert acl.returncode == 0
    admin("user", "set", "pgvn-test", "acl-user", "--key-file", bad_acl, input=password, expected=5)
    admin("user", "disable", "pgvn-test", "测试用户")
    digest(key, expected=1)
    run(PROBE, ["credential-guard", "pgvn-test", "测试用户", "1", "0"], role="pgvn_service", expected=1)
    admin("user", "set", "pgvn-test", "测试用户", "--key-file", key, input=password)
    assert digest(key)["generation"] == 3
    # Hold the guard's row lock inside a real SQL transaction, then revoke.
    held = subprocess.Popen([str(PROBE), "credential-guard", "pgvn-test", "测试用户", "3", "2500"], cwd=EMPTY,
        env=dict(os.environ, PGVN_DSN=dsn("pgvn_service")), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(held.stdout.readline)
            try:
                assert future.result(timeout=10).strip() == b"locked"
            except Exception:
                held.kill()
                raise
        start = time.monotonic()
        admin("user", "disable", "pgvn-test", "测试用户")
        blocked = time.monotonic() - start
        assert blocked >= 1.5, blocked
        _, stderr = held.communicate(timeout=10)
        assert held.returncode == 0, stderr
    finally:
        if held.poll() is None:
            held.kill()
            held.communicate(timeout=10)
    run(PROBE, ["credential-guard", "pgvn-test", "测试用户", "3", "0"], role="pgvn_service", expected=1)
    new_key=WORK/"credential-v2.key"
    admin("key","create",new_key,"test-v2")
    # Rotation preserves a disabled account and invalidates old generations.
    admin("user","rekey","pgvn-test","测试用户","--key-file",new_key,"--old-key-file",key)
    rotated=next(row for row in admin("user","list","pgvn-test") if row["username"]=="测试用户")
    assert rotated["key_id"]=="test-v2" and rotated["generation"]==5 and not rotated["enabled"]
    digest(new_key,expected=1)
    admin("user","set","pgvn-test","测试用户","--key-file",new_key,input=password)
    assert digest(new_key)=={"generation":6,"sha1":hashlib.sha1(password).hexdigest()}
    digest(key,expected=1)
    admin("user","rekey","pgvn-test","测试用户","--key-file",new_key,"--old-key-file",new_key,expected=2)
    identities={"Case","case","\u00e9","e\u0301"}
    for identity in identities:
        admin("user","set","pgvn-test",identity,"--key-file",key,input=password)
    assert identities <= {row["username"] for row in admin("user","list","pgvn-test")}
    assert sql('''SELECT count(*)=2 FROM pg_attribute WHERE attrelid='pgvn.credentials'::regclass
        AND attname IN ('identity_domain','username') AND attcollation='pg_catalog."C"'::regcollation''')=="t"
    admin("user","rekey","pgvn-test","Case","--key-file",new_key,"--old-key-file",other,expected=5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        rotations=list(executor.map(lambda _:admin("user","rekey","pgvn-test","Case","--key-file",new_key,
            "--old-key-file",key,expected=(0,5,7)),range(2)))
    assert sum(row["ok"] is True for row in rotations)==1
    assert digest(new_key,"Case")["generation"]==2
    assert digest(new_key,"case",previous=key)["generation"]==1
    digest(new_key,"case",expected=1)
    digest(new_key,"case",previous=other,expected=1)
    assert list(EMPTY.iterdir()) == []
    return {"schema": 4, "non_superuser_migration": True, "native_encrypt_decrypt": True,
        "unicode_password_and_identity": True, "key_overwrite_refused": True,
        "wrong_key_tag_and_identity_rejected": True, "broad_acl_rejected": True,
        "no_plaintext_password_in_database_or_command_line": True,
        "least_privilege_grants": True, "invalid_password_rollback": True,
        "revocation_and_generation": True, "publication_row_lock_blocks_revocation_seconds": blocked,
        "disabled_account_rekey_preserves_state":True,"dual_key_read_and_old_key_removal":True,
        "concurrent_rekey_single_winner":True,"case_and_unicode_identities_distinct":True,
        "pathless_working_directory_empty": True}


def main():
    start = time.monotonic()
    try:
        record = {"status": "PASS", "details": checks()}
    except Exception:
        record = {"status": "FAIL", "error": traceback.format_exc()}
    record.update(test_id=TEST_ID, component="M3 credentials", milestone_complete=False,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(), database=DATABASE,
        elapsed_seconds=time.monotonic() - start,
        pgvnadmin_sha256=hashlib.sha256(ADMIN.read_bytes()).hexdigest())
    target = ROOT / "artifacts/pgvn/tests"
    target.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, indent=2) + "\n"
    (target / (TEST_ID + ".json")).write_text(data, encoding="utf-8")
    (target / "m3-credentials-latest.json").write_text(data, encoding="utf-8")
    print(data)
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
