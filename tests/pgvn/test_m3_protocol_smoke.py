"""Development smoke test for two native SVN nodes. Not the full M3 gate."""
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback
from urllib.parse import quote
import test_m3_credentials as setup
from fixture import ROOT, RUNTIME
from svnkit_client import SvnKit

WORK = setup.WORK
EMPTY = setup.EMPTY
SERVER = RUNTIME / "bin/pgvnserve.exe"
CONFIG = json.loads((ROOT / "config/build.local.json").read_text(encoding="utf-8"))
SVN = __import__("pathlib").Path(CONFIG["svn_bin"])
PASSWORD = secrets.token_urlsafe(32).encode("ascii")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def client(tool, *args, user="writer", password=PASSWORD, expected=0, timeout=45):
    result = subprocess.run([str(SVN / (tool + ".exe")), *map(str, args), "--non-interactive",
        "--no-auth-cache", "--config-dir", str(WORK / "svn-client-config"), "--username", user,
        "--password-from-stdin"], input=password + b"\r\n", cwd=EMPTY, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
    allowed = expected if isinstance(expected, tuple) else (expected,)
    assert result.returncode in allowed, (tool, args, result.returncode, result.stderr[:2500])
    return result.stdout


class Node:
    def __init__(self, name, key):
        self.name = name
        self.svn_port = port()
        self.monitor_port = port()
        self.config = WORK / (name + ".ini")
        self.config.write_text("[pgvn]\nconnection = " + setup.dsn("pgvn_service") +
            f"\ncredential-key-file = {key.as_posix()}\nidentity-domain = pg vn test" +
            f"\nlisten-host = 127.0.0.1\nlisten-port = {self.svn_port}\n", encoding="utf-8")
        self.process = None
        self.log = None

    def start(self):
        self.log = (WORK / (self.name + ".log")).open("ab", buffering=0)
        self.process = subprocess.Popen([str(SERVER), "--config", str(self.config), "--monitor-url",
            f"http://127.0.0.1:{self.monitor_port}/"], cwd=EMPTY, stdout=self.log, stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.poll() is None, (self.name, (WORK / (self.name + ".log")).read_bytes())
            connection = http.client.HTTPConnection("127.0.0.1", self.monitor_port, timeout=1)
            try:
                connection.request("GET", "/health/live")
                response = connection.getresponse()
                if response.status == 200:
                    response.read()
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        raise AssertionError("Node did not start: " + self.name)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=15)
        if self.log:
            self.log.close()
            self.log = None

    def url(self, route):
        return f"svn://127.0.0.1:{self.svn_port}" + quote(route, safe="/")


def checks():
    setup.setup_database()
    key = WORK / "protocol.key"
    setup.admin("key", "create", key, "protocol-test-v1")
    for user in ("writer", "reader"):
        setup.admin("user", "set", "pg vn test", user, "--key-file", key, input=PASSWORD)
    route = "/协议/测试库"
    repository = setup.admin("repo", "create", "protocol-test", "--route", route)
    rules = WORK / "authz.conf"
    rules.write_text("[/]\nwriter = rw\nreader = r\n[protocol-test:/secret]\nreader =\n", encoding="utf-8")
    setup.admin("config", "publish", repository["id"], "--expected-generation", "1", "--authz", rules)
    a, b = Node("node-a", key), Node("node-b", key)
    try:
        a.start()
        b.start()
        first, second = a.url(route), b.url(route)
        client("svn", "info", "--xml", first, password=b"wrong-password", expected=1)
        client("svn", "info", "--xml", a.url("/missing"), expected=1)
        info = client("svn", "info", "--xml", first)
        assert repository["uuid"].encode() in info
        content = WORK / "payload.bin"
        content.write_bytes(bytes(range(256)) * 4097 + "中文内容\n".encode())
        client("svnmucc", "-m", "initial", "mkdir", first + "/trunk", "mkdir", first + "/secret",
            "put", content, first + "/trunk/payload.bin", "put", content, first + "/secret/hidden.bin")
        assert client("svn", "cat", second + "/trunk/payload.bin") == content.read_bytes()
        reader = client("svn", "list", "--xml", "-R", second, user="reader")
        assert b"payload.bin" in reader and b"hidden.bin" not in reader
        client("svn", "cat", second + "/secret/hidden.bin", user="reader", expected=1)
        client("svnmucc", "-m", "forbidden", "mkdir", second + "/reader-write", user="reader", expected=1)
        wc_a, wc_b = WORK / "wc-a", WORK / "wc-b"
        client("svn", "checkout", first, wc_a)
        client("svn", "checkout", second, wc_b)
        changed = content.read_bytes() + os.urandom(65537)
        (wc_a / "trunk/payload.bin").write_bytes(changed)
        client("svn", "commit", wc_a, "-m", "changed")
        client("svn", "update", wc_b)
        assert (wc_b / "trunk/payload.bin").read_bytes() == changed
        client("svn", "propset", "--revprop", "-r", "2", "pgvn:check", "shared-value", first, expected=1)
        hooks = WORK / "revprop-hooks.json"
        hooks.write_text('{"pre-revprop-change":"allow"}', encoding="utf-8")
        setup.admin("config", "publish", repository["id"], "--expected-generation", "2", "--authz", rules, "--hooks", hooks)
        client("svn", "propset", "--revprop", "-r", "2", "pgvn:check", "shared-value", first)
        assert client("svn", "propget", "--strict", "--revprop", "-r", "2", "pgvn:check", second) == b"shared-value"
        client("svn", "lock", first + "/trunk/payload.bin", "-m", "shared-lock")
        client("svn", "lock", second + "/trunk/payload.bin", "-m", "conflict", expected=1)
        client("svn", "unlock", "--force", second + "/trunk/payload.bin")
        # Node A's process can be lost while B continues against the same DB.
        a.stop()
        client("svnmucc", "-m", "after-node-loss", "mkdir", second + "/survived")
        a.start()
        assert b"survived" in client("svn", "list", first)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            outputs = list(executor.map(lambda i: client("svn", "info", "--xml", first if i % 2 else second), range(24)))
        assert all(repository["uuid"].encode() in output for output in outputs)
        svnkit = SvnKit(WORK, (first, second), "writer", PASSWORD)
        assert svnkit.roundtrip(wc_a / "trunk/payload.bin")["revision"] == 4
        svnkit.revoke("hold-read", lambda: setup.admin("config", "publish", repository["id"],
            "--expected-generation", "3", "--authz", rules))
        # Terminate only connections to this test's database. No installed
        # service or other test DB is stopped. Both control sessions reconnect.
        setup.fixture.assert_ours()
        setup.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='pgvn' "
            f"AND datname='{setup.DATABASE}' AND usename='pgvn_service' AND pid<>pg_backend_pid()")
        svnkit.expect_database_failure()
        recovery_attempts = []
        for url in (first, second):
            for attempt in range(1, 5):
                output = client("svn", "info", "--xml", url, expected=(0, 1))
                if repository["uuid"].encode() in output:
                    recovery_attempts.append(attempt)
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Node did not reconnect after its DB sessions were terminated")
        svnkit.revoke("hold-read", lambda: setup.admin("user", "disable", "pg vn test", "writer"))
        client("svn", "info", first, expected=1)
        client("svn", "info", second, expected=1)
        setup.admin("user", "set", "pg vn test", "writer", "--key-file", key, input=PASSWORD)
        svnkit.revoke("hold-commit", lambda: setup.admin("user", "disable", "pg vn test", "writer"))
        setup.admin("user", "set", "pg vn test", "writer", "--key-file", key, input=PASSWORD)
        # Deploy two readable versions on each node before re-encrypting any
        # accounts. The peer remains available throughout the rolling restart.
        new_key=WORK/"protocol-v2.key"
        setup.admin("key","create",new_key,"protocol-test-v2")
        for node,peer in ((a,second),(b,first)):
            node.stop(); client("svn","info",peer)
            text=node.config.read_text(encoding="utf-8").replace("credential-key-file = "+key.as_posix(),"credential-key-file = "+new_key.as_posix())
            node.config.write_text(text+"credential-previous-key-file = "+key.as_posix()+"\n",encoding="utf-8")
            node.start(); client("svn","info",first); client("svn","info",second)
        for user in ("writer","reader"):
            setup.admin("user","rekey","pg vn test",user,"--key-file",new_key,"--old-key-file",key)
        for node,peer in ((a,second),(b,first)):
            node.stop(); client("svn","info",peer)
            text="\n".join(line for line in node.config.read_text(encoding="utf-8").splitlines() if not line.startswith("credential-previous-key-file"))+"\n"
            node.config.write_text(text,encoding="utf-8"); node.start()
            client("svn","info",first); client("svn","info",second)
        assert setup.sql("SELECT count(*) FROM pgvn.credentials WHERE key_id<>'protocol-test-v2'")=="0"
        # Access levels and realms come from the repository's versioned policy.
        configured=setup.admin("repo","create","protocol-settings","--route","/settings")
        settings_rules=WORK/"settings-authz.conf"
        settings_rules.write_text("[/]\n* = r\nsettings-only = rw\n",encoding="utf-8")
        settings_path=WORK/"protocol-settings.json"
        settings={"identity_domain":"独立身份域","anonymous_access":"read","authenticated_access":"read"}
        def publish_settings():
            settings_path.write_text(json.dumps(settings,ensure_ascii=False),encoding="utf-8")
            generation=setup.admin("config","show",configured["id"])["generation"]
            setup.admin("config","publish",configured["id"],"--expected-generation",generation,"--authz",settings_rules,"--settings",settings_path)
        setup.admin("user","set","独立身份域","settings-only","--key-file",new_key,input=PASSWORD)
        publish_settings()
        target=a.url("/settings"); peer_target=b.url("/settings")
        client("svn","info",target,user="missing-user",password=b"wrong")
        client("svnmucc","-m","read-only-policy","mkdir",target+"/denied",user="settings-only",expected=1)
        settings["authenticated_access"]="write"; publish_settings()
        client("svnmucc","-m","custom-domain","mkdir",peer_target+"/custom-domain",user="settings-only")
        client("svnmucc","-m","anonymous-denied","mkdir",target+"/denied-anon",user="missing-user",password=b"wrong",expected=1)
        settings["anonymous_access"]="write"; settings["authenticated_access"]="none"
        settings_rules.write_text("[/]\n* = rw\n",encoding="utf-8"); publish_settings()
        client("svnmucc","-m","explicit-anonymous-write","mkdir",target+"/anonymous","put",content,target+"/anonymous/file.bin",user="missing-user",password=b"wrong")
        client("svn","lock",target+"/anonymous/file.bin",user="missing-user",password=b"wrong",expected=1)
        # A process-wide read-only setting overrides the writable policy.
        a.stop(); a.config.write_text(a.config.read_text(encoding="utf-8")+"read-only = true\n",encoding="utf-8"); a.start()
        client("svnmucc","-m","global-read-only","mkdir",target+"/denied-global",expected=1)
        client("svnmucc","-m","writable-peer","mkdir",peer_target+"/peer-writes")
        assert b"peer-writes" in client("svn","list",target)
        a.stop(); a.config.write_text(a.config.read_text(encoding="utf-8")+"max-response-bytes = 4096\n",encoding="utf-8"); a.start()
        client("svn","cat",first+"/trunk/payload.bin",expected=1)
        assert client("svn","cat",second+"/trunk/payload.bin")==changed
        b.stop(); b.config.write_text(b.config.read_text(encoding="utf-8")+"max-request-bytes = 4096\n",encoding="utf-8"); b.start()
        budget_file=WORK/"over-small-budget.bin"; budget_file.write_bytes(os.urandom(16384))
        client("svnmucc","-m","request-over-budget","put",budget_file,peer_target+"/over-budget.bin",expected=1)
        assert setup.admin("look","youngest",configured["id"])["youngest"]==3
        b.stop()
        text=b.config.read_text(encoding="utf-8").replace("max-request-bytes = 4096","max-request-bytes = 0")+"max-response-bytes = 0\n"
        b.config.write_text(text,encoding="utf-8"); b.start()
        large=WORK/"over-default-budget.bin"; large_bytes=65*1024*1024+1
        chunk=os.urandom(65536)
        with large.open("wb") as output:
            for _ in range(large_bytes//len(chunk)): output.write(chunk)
            output.write(chunk[:large_bytes%len(chunk)])
        client("svnmucc","-m","explicit-unlimited-budget","put",large,peer_target+"/large.bin",timeout=180)
        actual=client("svn","cat",peer_target+"/large.bin",timeout=180)
        assert len(actual)==large_bytes and hashlib.sha256(actual).digest()==hashlib.sha256(large.read_bytes()).digest()
        del actual
        setup.admin("verify",configured["id"])
        assert setup.admin("look","youngest",configured["id"])["youngest"]==4
        # NULs must not turn a longer URL into a valid repository prefix.
        # This is a direct RA SVN handshake, bypassing client URL validation.
        for suffix in (b"%00suffix",b"\0suffix",b"%2500suffix"):
            with socket.create_connection(("127.0.0.1",b.svn_port),timeout=3) as raw:
                raw.settimeout(3); assert raw.recv(4096)
                url=second.encode("ascii")+suffix
                raw.sendall(b"( 2 ( edit-pipeline ) "+str(len(url)).encode()+b":"+url+b" ) ")
                response=b""
                try:
                    while True:
                        data=raw.recv(4096)
                        if not data: break
                        response+=data
                        if b"failure" in response or b"CRAM-MD5" in response: break
                except ConnectionResetError: pass
                assert b"CRAM-MD5" not in response,(suffix,response)
        literal=setup.admin("repo","create","literal-percent","--route","/literal%00")
        setup.admin("config","publish",literal["id"],"--expected-generation",1,"--authz",rules)
        assert literal["uuid"].encode() in client("svn","info","--xml",b.url("/literal%00"))
        # A real idle connection must time out without any shutdown request.
        with socket.create_connection(("127.0.0.1",b.svn_port),timeout=3) as raw:
            raw.settimeout(36); assert raw.recv(4096)
            started=time.monotonic()
            try: data=raw.recv(4096)
            except ConnectionResetError: data=b""
            idle_seconds=time.monotonic()-started
            assert (not data or b"failure" in data) and 28<=idle_seconds<36,(idle_seconds,data)
        setup.admin("verify", repository["id"])
        assert setup.admin("look", "youngest", repository["id"])["youngest"] == 4
        assert list(EMPTY.iterdir()) == []
        return {"two_process_nodes": True, "original_svn_and_svnmucc": True,
            "database_routes_and_uuid": True, "cram_md5_success_and_failure": True,
            "path_authz_and_read_only_user": True, "binary_roundtrip_and_working_copy_update": True,
            "cross_node_lock_conflict_and_unlock": True, "node_loss_and_restart": True,
            "explicit_hook_required_for_revprops": True, "cross_node_revprops": True,
            "control_connection_recovery_attempts": recovery_attempts,
            "database_error_uses_svnkit_known_code":True,
            "svnkit": svnkit.evidence, "svnkit_binary_commit_revprops": True,
            "svnkit_persistent_session_config_and_credential_revocation": True,
            "svnkit_inflight_commit_revocation": True,
            "rolling_deployment_key_rotation":True,"previous_key_removed_after_rekey":True,
            "versioned_identity_domain_and_access_levels":True,"explicit_anonymous_policy":True,
            "process_read_only_overrides_repository":True,
            "command_byte_limits_and_explicit_unlimited":True,"large_blob_bytes":large_bytes,
            "direct_protocol_nul_rejected_and_percent_decoded_once":True,"idle_timeout_seconds":idle_seconds,
            "parallel_reads": 24, "credential_revocation_visible_on_both_nodes": True,
            "full_verify": True, "youngest": 4, "pathless_working_directory_empty": True}
    finally:
        a.stop()
        b.stop()


def main():
    start = time.monotonic()
    try:
        report = {"status": "PASS", "details": checks()}
    except Exception:
        report = {"status": "FAIL", "error": traceback.format_exc()}
    report.update(test_id=setup.TEST_ID.replace("credentials", "protocol-smoke"),
        component="M3 protocol smoke", milestone_complete=False, database=setup.DATABASE,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - start,
        pgvnserve_sha256=hashlib.sha256(SERVER.read_bytes()).hexdigest())
    target = ROOT / "artifacts/pgvn/tests"
    target.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, indent=2) + "\n"
    (target / (report["test_id"] + ".json")).write_text(data, encoding="utf-8")
    (target / "m3-protocol-smoke-latest.json").write_text(data, encoding="utf-8")
    print(data)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
