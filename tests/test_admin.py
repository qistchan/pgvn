"""Native dump/load, corruption detection and repository freeze gates."""
import concurrent.futures
import configparser
import hashlib
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from test_p1 import NativeTreeTests, ROOT, BIN
from test_p2 import CompatibilityTests
from test_p0 import sql


class AdminTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest
    checkout = NativeTreeTests.checkout
    put = NativeTreeTests.put
    seed = CompatibilityTests.seed

    def load(self, repo, data):
        result = subprocess.run([str(BIN / "svnadmin.exe"), "load", "--force-uuid", str(repo)],
                                input=data, env=self.env, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf8", "replace"))

    def test_dump_load_both_directions_and_pack_preserve_history(self):
        source, url, wc = self.seed("fsfs")
        self.run_tool("svn", "copy", url + "/trunk", url + "/branches/one", "-m", "copy")
        self.run_tool("svn", "update", wc)
        self.put(wc, "branches/one/sub/file.txt", b"branch edit\n")
        self.run_tool("svn", "propset", "svn:mergeinfo", "/trunk:1-2", wc / "branches/one")
        self.run_tool("svn", "delete", wc / "branches/one/other.txt")
        self.put(wc, "branches/one/new.bin", bytes(range(256)) * 5000)
        self.run_tool("svn", "add", wc / "branches/one/new.bin")
        self.run_tool("svn", "commit", wc, "-m", "branch mutations")
        archive = self.run_tool("svnadmin", "dump", source, "--deltas").stdout
        postgres, _ = self.create("pg")
        self.load(postgres, archive)
        self.run_tool("svnadmin", "verify", postgres)
        self.run_tool("svnadmin", "pack", postgres)
        self.run_tool("svnadmin", "upgrade", postgres)
        self.run_tool("svnadmin", "recover", postgres)
        self.run_tool("svnadmin", "verify", postgres)
        archive = self.run_tool("svnadmin", "dump", postgres, "--deltas").stdout
        restored = self.fixture / "restored"
        self.run_tool("svnadmin", "create", restored, "--fs-type", "fsfs")
        self.load(restored, archive)
        self.run_tool("svnadmin", "verify", restored)
        self.assertEqual(self.youngest(source), self.youngest(postgres))
        self.assertEqual(self.youngest(source), self.youngest(restored))
        for revision in range(4):
            for actual in (postgres, restored):
                self.assertEqual(self.run_tool("svnlook", "tree", "--full-paths", "-r", revision, source).stdout,
                                 self.run_tool("svnlook", "tree", "--full-paths", "-r", revision, actual).stdout)
                self.assertEqual(self.run_tool("svnlook", "changed", "--copy-info", "-r", revision, source).stdout,
                                 self.run_tool("svnlook", "changed", "--copy-info", "-r", revision, actual).stdout)
                self.assertEqual(self.run_tool("svnlook", "uuid", source).stdout, self.run_tool("svnlook", "uuid", actual).stdout)
        for actual in (postgres, restored):
            self.assertEqual(self.run_tool("svnlook", "cat", source, "branches/one/new.bin").stdout,
                             self.run_tool("svnlook", "cat", actual, "branches/one/new.bin").stdout)
            self.assertEqual(self.run_tool("svnlook", "history", source, "branches/one/sub/file.txt").stdout,
                             self.run_tool("svnlook", "history", actual, "branches/one/sub/file.txt").stdout)

    def test_verify_detects_corrupt_fulltext(self):
        repo, url = self.create()
        wc = self.checkout(url, "wc")
        self.put(wc, "file.txt", b"checksum evidence\n")
        self.run_tool("svn", "add", wc / "file.txt")
        self.run_tool("svn", "commit", wc, "-m", "data")
        config = configparser.ConfigParser()
        config.read(repo / "db/pgfs.conf")
        repository = config["pgfs"]["repository"]
        content = sql("SELECT content_id FROM pgfs.node_revisions WHERE repository_id='" + repository +
                      "' AND created_path='/file.txt' AND content_id IS NOT NULL LIMIT 1;").decode("ascii")
        mutate = ("UPDATE pgfs.content_chunks SET data=set_byte(data,0,get_byte(data,0)#255) "
                  "WHERE repository_id='" + repository + "' AND content_id=" + content + " AND chunk_number=0;")
        self.run_tool("svnadmin", "verify", repo)
        sql(mutate)
        try:
            error = self.run_tool("svnadmin", "verify", repo, ok=False)
            self.assertIn(b"checksum mismatch", error.stderr.lower())
        finally:
            sql(mutate)
        self.run_tool("svnadmin", "verify", repo)

    def test_verify_checks_corrupt_deleted_history_and_honors_revision_range(self):
        repo, url = self.create()
        wc = self.checkout(url, "wc")
        self.put(wc, "historical.txt", b"historical checksum evidence\n")
        self.run_tool("svn", "add", wc / "historical.txt")
        self.run_tool("svn", "commit", wc, "-m", "historical file")
        self.run_tool("svn", "mkdir", url + "/later", "-m", "reuse immutable file")
        self.run_tool("svn", "update", wc)
        self.run_tool("svn", "delete", wc / "historical.txt")
        self.run_tool("svn", "commit", wc, "-m", "remove file from HEAD")
        self.assertEqual(self.youngest(repo), 3)
        config = configparser.ConfigParser()
        config.read(repo / "db/pgfs.conf")
        repository = config["pgfs"]["repository"]
        content = sql("SELECT content_id FROM pgfs.node_revisions WHERE repository_id='" + repository +
                      "' AND created_path='/historical.txt' AND content_id IS NOT NULL LIMIT 1;").decode("ascii").strip()
        self.assertTrue(content.isdigit())
        mutate = ("UPDATE pgfs.content_chunks SET data=set_byte(data,0,get_byte(data,0)#255) "
                  "WHERE repository_id='" + repository + "' AND content_id=" + content + " AND chunk_number=0;")
        self.run_tool("svnadmin", "verify", repo)
        sql(mutate)
        try:
            self.run_tool("svnadmin", "verify", "-r", "3:3", repo)
            error = self.run_tool("svnadmin", "verify", "-r", "1:3", repo, ok=False)
            self.assertIn(b"checksum mismatch", error.stderr.lower())
        finally:
            sql(mutate)
        self.run_tool("svnadmin", "verify", repo)

    def test_verify_transaction_root_with_inherited_and_staged_content(self):
        repo, url, wc = self.seed("pg")
        txn = self.run_tool("fs_probe", "begin", repo, "1").stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "verify-txn", repo, txn)
        payload = self.fixture / "staged.bin"
        payload.write_bytes(b"staged\0content\xff")
        self.run_tool("fs_probe", "write", repo, txn, "/trunk/sub/file.txt", payload)
        self.run_tool("fs_probe", "verify-txn", repo, txn)
        self.run_tool("fs_probe", "commit", repo, txn)
        self.assertEqual(self.youngest(repo), 2)
        self.run_tool("svnadmin", "verify", repo)
        self.assertEqual(self.run_tool("svnlook", "cat", repo, "trunk/sub/file.txt").stdout, payload.read_bytes())

    def test_freeze_blocks_publication_and_releases_it_after_callback(self):
        repo, url = self.create()
        marker, release = self.fixture / "frozen", self.fixture / "release"
        with (self.fixture / "freeze.log").open("wb") as log:
            freeze = subprocess.Popen([str(BIN / "svnadmin.exe"), "freeze", str(repo), "--", sys.executable,
                                       str(ROOT / "tests/hold_freeze.py"), str(marker), str(release)],
                                      env=self.env, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and freeze.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    commit = executor.submit(self.run_tool, "svn", "mkdir", url + "/after-freeze", "-m", "commit waits")
                    deadline = time.monotonic() + 5
                    blocked = False
                    try:
                        while time.monotonic() < deadline:
                            if int(sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock';")):
                                blocked = True
                                break
                            time.sleep(0.02)
                        self.assertTrue(blocked, "No PostgreSQL lock wait was observed")
                        self.assertEqual(self.youngest(repo), 0)
                        self.assertFalse(commit.done())
                    finally:
                        release.write_text("release", encoding="ascii")
                    self.assertEqual(freeze.wait(timeout=10), 0)
                    commit.result(timeout=15)
                self.assertEqual(self.youngest(repo), 1)
            finally:
                release.write_text("release", encoding="ascii")
                if freeze.poll() is None:
                    freeze.terminate()
                    freeze.wait(timeout=10)

    def test_hotcopy_full_incremental_independence_and_divergence(self):
        repo, url, wc = self.seed("pg")
        self.run_tool("svn", "copy", url + "/trunk", url + "/branches/one", "-m", "copy")
        self.run_tool("svn", "lock", wc / "trunk/sub/file.txt", "--username", "alice")
        archive = self.fixture / "hotcopy"
        self.run_tool("svnadmin", "hotcopy", repo, archive)
        self.run_tool("svnadmin", "verify", archive)
        self.assertEqual(self.youngest(archive), 2)
        config = configparser.ConfigParser()
        config.read(repo / "db/pgfs.conf")
        source_id = config["pgfs"]["repository"]
        config.read(archive / "db/pgfs.conf")
        self.assertNotEqual(source_id, config["pgfs"]["repository"])
        self.assertEqual(self.run_tool("svnlook", "uuid", repo).stdout,
                         self.run_tool("svnlook", "uuid", archive).stdout)
        self.assertIn(b"alice", self.run_tool("svnadmin", "lslocks", archive).stdout)
        self.run_tool("svn", "mkdir", url + "/later", "-m", "source grows")
        value = self.fixture / "revprop.txt"
        value.write_bytes(b"hotcopy mutable property\n")
        self.run_tool("svnadmin", "setrevprop", repo, "-r", "1", "test:mutable", value)
        for _ in range(2):
            self.run_tool("svnadmin", "hotcopy", repo, archive, "--incremental")
            self.assertEqual(self.youngest(archive), 3)
            self.run_tool("svnadmin", "verify", archive)
        self.assertEqual(self.run_tool("svnlook", "propget", "--revprop", "-r", "1", archive, "test:mutable").stdout,
                         value.read_bytes())
        self.run_tool("svn", "mkdir", archive.as_uri() + "/copy-only", "-m", "independent copy")
        self.assertEqual(self.youngest(archive), 4)
        self.assertEqual(self.youngest(repo), 3)
        self.assertNotIn(b"copy-only", self.run_tool("svnlook", "tree", repo).stdout)
        self.run_tool("svnadmin", "hotcopy", repo, archive, "--incremental", ok=False)
        self.assertEqual(self.youngest(archive), 4)
        self.run_tool("svnadmin", "verify", archive)

    def test_delete_fs_removes_only_its_storage_namespace(self):
        repo, url, wc = self.seed("pg")
        archive = self.fixture / "survivor"
        self.run_tool("svnadmin", "hotcopy", repo, archive)
        config = configparser.ConfigParser()
        config.read(repo / "db/pgfs.conf")
        repository = config["pgfs"]["repository"]
        self.run_tool("fs_probe", "delete-fs", repo)
        self.assertFalse((repo / "db").exists())
        self.assertEqual(sql("SELECT count(*) FROM pgfs.repositories WHERE repository_id='" + repository + "';"), b"0")
        self.assertEqual(self.youngest(archive), 1)
        self.run_tool("svnadmin", "verify", archive)
        self.assertEqual(self.run_tool("svnlook", "cat", archive, "trunk/sub/file.txt").stdout, b"base\nline two\n")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AdminTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    (ROOT / "artifacts/tests/admin-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
