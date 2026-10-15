"""End-to-end native svnserve / mod_dav_svn and installed SVNKit gates."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import unittest
from datetime import datetime, timezone
from test_p1 import NativeTreeTests, ROOT, BIN

APACHE_SOURCE = Path(r"C:\Polarion\bundled\apache")
ORIGINAL_SVN = Path(r"C:\Polarion\bundled\svn/bin/svn.exe")
APACHE = ROOT / "runtime/apache"
JAVA = Path(r"C:\Program Files\Eclipse Adoptium\jdk-21.0.11.10-hotspot/bin")
PLUGINS = Path(r"C:\Polarion\polarion/plugins")
JARS = [PLUGINS / "org.tmatesoft.svnkit_1.10.1.r10777.jar",
        PLUGINS / "com.trilead.ssh2_1.0.0.build222_185.jar",
        PLUGINS / "org.tmatesoft.sqljet_1.1.12.r1336.jar",
        PLUGINS / "antlr-runtime_3.5.3.jar",
        PLUGINS / "com.jcraft.jsch_0.1.51.jar",
        PLUGINS / "com.sun.jna_5.13.0.v20230812-1000"]
CLASSES = ROOT / "build/svnkit"
CLASSPATH = os.pathsep.join(str(p) for p in [CLASSES, *JARS])


def prepare_runtimes():
    (APACHE / "bin").mkdir(parents=True, exist_ok=True)
    (APACHE / "modules").mkdir(exist_ok=True)
    # Isolated copies only. All SVN DLLs come from the already-tested runtime.
    for path in (APACHE_SOURCE / "bin").iterdir():
        if path.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(path, APACHE / "bin" / path.name)
    for path in BIN.glob("*.dll"):
        shutil.copy2(path, APACHE / "bin" / path.name)
    for path in (APACHE_SOURCE / "modules").glob("*.so"):
        shutil.copy2(path, APACHE / "modules" / path.name)
    CLASSES.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(JAVA / "javac.exe"), "-encoding", "UTF-8", "-cp", CLASSPATH,
                             "-d", str(CLASSES), str(ROOT / "tests/SVNKitProbe.java")],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf8", "replace"))


class ServiceTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest
    checkout = NativeTreeTests.checkout
    put = NativeTreeTests.put

    @classmethod
    def setUpClass(cls):
        prepare_runtimes()

    def client(self, *args, user="alice", ok=True):
        # Network clients use the untouched Polarion SVN distribution. Only
        # the server runtime loads PgFS; the client needs no modified DLLs.
        result = subprocess.run([str(ORIGINAL_SVN), *map(str, args), "--username", user,
                                 "--password", "pgfs-test", "--no-auth-cache", "--non-interactive"],
                                env=self.env, capture_output=True, timeout=60)
        if ok and result.returncode:
            self.fail(result.stderr.decode("utf8", "replace"))
        if not ok:
            self.assertNotEqual(result.returncode, 0)
        return result

    def run_protocol(self, protocol, port):
        repo, local = self.create()
        authz = self.fixture / "authz"
        authz.write_text("[pg:/]\nalice = rw\nreader = r\n[pg:/secret]\nalice = rw\nreader =\n", encoding="utf8")
        config = repo / "conf/svnserve.conf"
        config.write_text("[general]\nanon-access = none\nauth-access = write\npassword-db = passwd\n"
                          "authz-db = " + str(authz) + "\nrealm = PgFS integration\n[sasl]\nuse-sasl = false\n", encoding="utf8")
        (repo / "conf/passwd").write_text("[users]\nalice = pgfs-test\nreader = pgfs-test\n", encoding="utf8")
        if protocol == "svn":
            program = BIN / "svnserve.exe"
            args = [str(program), "--daemon", "--foreground", "--listen-host", "127.0.0.1",
                    "--listen-port", str(port), "--root", str(self.fixture)]
            url = f"svn://127.0.0.1:{port}/pg"
        else:
            program = APACHE / "bin/httpd.exe"
            password = self.fixture / "http.passwd"
            for index, user in enumerate(("alice", "reader")):
                result = subprocess.run([str(APACHE / "bin/htpasswd.exe"), "-bc" if index == 0 else "-b",
                                         str(password), user, "pgfs-test"], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            config = self.fixture / "httpd.conf"
            logs = self.fixture / "http-logs"
            logs.mkdir()
            conf = f'''ServerRoot "{APACHE.as_posix()}"
Listen 127.0.0.1:{port}
ServerName 127.0.0.1:{port}
PidFile "{(logs / 'httpd.pid').as_posix()}"
ErrorLog "{(logs / 'error.log').as_posix()}"
LogLevel warn
LoadModule authn_core_module modules/mod_authn_core.so
LoadModule authn_file_module modules/mod_authn_file.so
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule authz_user_module modules/mod_authz_user.so
LoadModule auth_basic_module modules/mod_auth_basic.so
LoadModule dav_module modules/mod_dav.so
LoadModule dav_svn_module modules/mod_dav_svn.so
LoadModule authz_svn_module modules/mod_authz_svn.so
<Location /svn>
  DAV svn
  SVNParentPath "{self.fixture.as_posix()}"
  AuthType Basic
  AuthName "PgFS integration"
  AuthBasicProvider file
  AuthUserFile "{password.as_posix()}"
  AuthzSVNAccessFile "{authz.as_posix()}"
  Require valid-user
</Location>
'''
            config.write_text(conf, encoding="utf8")
            result = subprocess.run([str(program), "-t", "-f", str(config)], env=self.env,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = [str(program), "-X", "-f", str(config)]
            url = f"http://127.0.0.1:{port}/svn/pg"
        with socket.socket() as check:
            self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0, "Refuse to reuse an occupied test port")
        launch_log = self.fixture / "server-output.log"
        with launch_log.open("wb") as output:
            process = subprocess.Popen(args, env=self.env, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                ready = False
                while time.monotonic() < deadline and process.poll() is None:
                    with socket.socket() as check:
                        if check.connect_ex(("127.0.0.1", port)) == 0:
                            ready = True
                            break
                    time.sleep(0.05)
                self.assertTrue(ready, launch_log.read_text(encoding="utf8", errors="replace"))
                wc = self.fixture / "service-wc"
                self.client("checkout", url, wc)
                payload = bytes(range(256)) * 5000
                self.put(wc, "file.bin", payload)
                self.put(wc, "secret/hidden.txt", b"private\n")
                self.client("add", "--force", wc)
                self.client("commit", wc, "-m", "native protocol write")
                self.assertEqual(self.youngest(repo), 1)
                self.assertEqual(self.client("cat", url + "/file.bin", user="reader").stdout, payload)
                self.client("cat", url + "/secret/hidden.txt", user="reader", ok=False)
                self.client("mkdir", url + "/reader-denied", "-m", "denied", user="reader", ok=False)
                self.assertEqual(self.youngest(repo), 1)
                self.client("copy", url + "/file.bin", url + "/copy.bin", "-m", "protocol copy")
                self.client("log", "--xml", url + "/copy.bin")
                self.client("lock", wc / "file.bin", "-m", "protocol lock")
                self.put(wc, "file.bin", payload + b"suffix")
                self.client("commit", wc, "-m", "locked protocol edit")
                self.assertEqual(self.youngest(repo), 3)
                self.assertEqual(self.client("cat", url + "/file.bin").stdout, payload + b"suffix")
                veto = repo / "hooks/pre-commit.bat"
                veto.write_text("@echo off\necho deliberate test veto 1>&2\nexit /b 1\n", encoding="ascii")
                self.client("mkdir", url + "/vetoed", "-m", "hook must veto", ok=False)
                self.assertEqual(self.youngest(repo), 3)
                veto.unlink()
                audit = self.fixture / "post-commit.log"
                (repo / "hooks/post-commit.bat").write_text('@echo off\necho %2 >>"' + str(audit) + '"\nexit /b 0\n', encoding="ascii")
                java = subprocess.run([str(JAVA / "java.exe"), "-Dsvnkit.useJNA=false", "-cp", CLASSPATH,
                                       "SVNKitProbe", url, "alice", "pgfs-test", str(self.fixture / "svnkit-config")],
                                      env=self.env, capture_output=True, timeout=45)
                self.assertEqual(java.returncode, 0, java.stderr.decode("utf8", "replace"))
                self.assertIn(b"bytes=1048833", java.stdout)
                self.assertEqual(self.youngest(repo), 4)
                self.assertEqual(audit.read_text().strip(), "4")
                self.assertEqual(self.run_tool("svnadmin", "lstxns", repo).stdout, b"")
                (ROOT / "artifacts/tests" / (protocol + "-service-fixture.json")).write_text(
                    json.dumps({"repository": str(repo), "protocol": protocol, "port": port,
                                "head": 4, "svnkit_stdout": java.stdout.decode("utf8").strip()}, indent=2) + "\n", encoding="utf8")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    def test_two_frontends_share_commits_revision_properties_and_locks(self):
        repo, _ = self.create()
        authz = self.fixture / "dual.authz"
        authz.write_text("[pg:/]\nalice = rw\nreader = r\n[pg:/secret]\nalice = rw\nreader =\n", encoding="utf8")
        (repo / "conf/svnserve.conf").write_text(
            "[general]\nanon-access = none\nauth-access = write\npassword-db = passwd\nauthz-db = " + str(authz) +
            "\nrealm = PgFS shared repository\n[sasl]\nuse-sasl = false\n", encoding="utf8")
        (repo / "conf/passwd").write_text("[users]\nalice = pgfs-test\nreader = pgfs-test\n", encoding="utf8")
        (repo / "hooks/pre-revprop-change.bat").write_text("@echo off\nexit /b 0\n", encoding="ascii")
        ports, processes, logs = [], [], []
        try:
            for index in range(2):
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                self.assertNotIn(port, ports)
                ports.append(port)
                output = (self.fixture / ("frontend-" + str(index) + ".log")).open("wb")
                logs.append(output)
                process = subprocess.Popen([str(BIN / "svnserve.exe"), "--daemon", "--foreground",
                    "--listen-host", "127.0.0.1", "--listen-port", str(port), "--root", str(self.fixture)],
                    env=self.env, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                processes.append(process)
                deadline = time.monotonic() + 15
                ready = False
                while time.monotonic() < deadline and process.poll() is None:
                    with socket.socket() as check:
                        if check.connect_ex(("127.0.0.1", port)) == 0:
                            ready = True
                            break
                    time.sleep(0.05)
                self.assertTrue(ready, "Dedicated frontend did not start")
            first, second = [f"svn://127.0.0.1:{port}/pg" for port in ports]
            wc = self.fixture / "dual-wc"
            self.client("checkout", first, wc)
            payload = b"two independent frontends\0\xff"
            self.put(wc, "public.bin", payload)
            self.put(wc, "secret/private.txt", b"restricted\n")
            self.client("add", "--force", wc)
            self.client("commit", wc, "-m", "publish through first frontend")
            self.assertEqual(self.client("cat", second + "/public.bin", user="reader").stdout, payload)
            self.client("cat", second + "/secret/private.txt", user="reader", ok=False)
            self.assertEqual(self.client("info", "--show-item", "revision", second).stdout.strip(), b"1")
            self.client("propset", "--revprop", "-r", "1", "test:shared", "first", first)
            self.assertEqual(self.client("propget", "--revprop", "-r", "1", "test:shared", second).stdout.strip(), b"first")
            self.client("propset", "--revprop", "-r", "1", "test:shared", "second", second)
            self.assertEqual(self.client("propget", "--revprop", "-r", "1", "test:shared", first).stdout.strip(), b"second")
            self.client("lock", wc / "public.bin", "-m", "lock through first frontend")
            self.assertIn(b"<lock>", self.client("info", "--xml", second + "/public.bin").stdout)
            self.client("delete", second + "/public.bin", "-m", "no lock token", ok=False)
            self.assertEqual(self.youngest(repo), 1)
            self.client("mkdir", second + "/from-second", "-m", "publish through second frontend")
            self.assertEqual(self.client("info", "--show-item", "revision", first).stdout.strip(), b"2")
            self.assertEqual(self.client("cat", first + "/public.bin").stdout, payload)
            self.client("unlock", "--force", second + "/public.bin")
            self.assertNotIn(b"<lock>", self.client("info", "--xml", first + "/public.bin").stdout)
            self.assertEqual(self.run_tool("svnadmin", "lstxns", repo).stdout, b"")
            self.run_tool("svnadmin", "verify", repo)
            (ROOT / "artifacts/tests/dual-service-fixture.json").write_text(json.dumps(
                {"repository":str(repo),"ports":ports,"head":2,"independent_frontends":2,
                 "commits_visible_both_directions":True,"revprops_visible_both_directions":True,
                 "shared_lock_enforcement":True,"restricted_read_denied":True}, indent=2) + "\n", encoding="utf8")
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            for output in logs:
                output.close()

    def test_svnserve_native_and_polarion_svnkit(self):
        self.run_protocol("svn", 13690)

    def test_apache_dav_native_and_polarion_svnkit(self):
        self.run_protocol("http", 13980)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ServiceTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest(),
              "svnkit_sha256": hashlib.sha256(JARS[0].read_bytes()).hexdigest()}
    report["original_svn_client_sha256"] = hashlib.sha256(ORIGINAL_SVN.read_bytes()).hexdigest()
    (ROOT / "artifacts/tests/services-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
