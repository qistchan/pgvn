"""Native SVN integration gates for persistent transactions and file trees."""
from pathlib import Path
import concurrent.futures
import hashlib
import json
import os
import subprocess
import unittest
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "runtime/svn/bin"
DSN = "host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable"


class NativeTreeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ROOT / "runtime/repos" / ("p1-" + uuid.uuid4().hex)
        self.fixture.mkdir(parents=True)
        self.env = dict(os.environ, PGFS_DSN=DSN)

    def run_tool(self, tool, *args, ok=True):
        if tool == "svn":
            args = (*args, "--non-interactive")
        result = subprocess.run([str(BIN / (tool + ".exe")), *map(str, args)],
                                env=self.env, capture_output=True, timeout=60)
        if ok and result.returncode:
            self.fail(f"{tool} {args}: {result.returncode}\n{result.stderr.decode('utf8', 'replace')}")
        if not ok:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def create(self, fs_type="pg"):
        repo = self.fixture / fs_type
        self.run_tool("svnadmin", "create", "--fs-type", fs_type, repo)
        return repo, repo.as_uri()

    def youngest(self, repo):
        return int(self.run_tool("svnlook", "youngest", repo).stdout)

    def checkout(self, url, name):
        wc = self.fixture / name
        self.run_tool("svn", "checkout", url, wc)
        return wc

    def put(self, wc, name, data):
        target = wc / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def test_binary_streams_properties_and_historical_reads(self):
        repo, url = self.create()
        wc = self.checkout(url, "wc")
        original = bytes(range(256)) * 12289
        names = {"目录/数据.bin": original, "empty.txt": b"", "Case.txt": b"upper\n",
                 "case.txt": b"lower\n", "normal.txt": b"first\n"}
        # Windows working copies cannot contain case-only siblings; repository
        # case sensitivity is tested below with URL operations instead.
        names.pop("case.txt")
        for name, data in names.items():
            self.put(wc, name, data)
        self.run_tool("svn", "add", "--force", wc)
        prop = self.fixture / "prop.bin"
        prop.write_bytes(b"a\0b\xff")
        self.run_tool("svn", "propset", "test:binary", "-F", prop, wc / "normal.txt")
        self.run_tool("svn", "propset", "test:empty", "", wc / "normal.txt")
        self.run_tool("svn", "commit", wc, "-m", "initial files")
        self.assertEqual(self.youngest(repo), 1)
        for name, data in names.items():
            self.assertEqual(self.run_tool("svn", "cat", url + "/" + quote(name), "-r", "1").stdout, data)
        self.assertEqual(self.run_tool("svnlook", "propget", repo, "test:binary", "normal.txt").stdout, prop.read_bytes())
        self.assertEqual(self.run_tool("svnlook", "propget", repo, "test:empty", "normal.txt").stdout, b"")
        second = b"prefix\0" + original[1000:] + b"\xffsuffix"
        self.put(wc, "目录/数据.bin", second)
        self.put(wc, "normal.txt", b"second\n")
        self.run_tool("svn", "delete", wc / "empty.txt")
        self.run_tool("svn", "commit", wc, "-m", "edit and delete")
        self.assertEqual(self.youngest(repo), 2)
        self.assertEqual(self.run_tool("svn", "cat", url + "/" + quote("目录/数据.bin"), "-r", "1").stdout, original)
        self.assertEqual(self.run_tool("svn", "cat", url + "/" + quote("目录/数据.bin")).stdout, second)
        self.assertEqual(self.run_tool("svnlook", "cat", "-r", "1", repo, "empty.txt").stdout, b"")
        self.run_tool("svnlook", "cat", "-r", "2", repo, "empty.txt", ok=False)
        other = self.checkout(url, "other")
        self.assertEqual((other / "目录/数据.bin").read_bytes(), second)
        self.assertFalse((other / "empty.txt").exists())
        changed = self.run_tool("svnlook", "changed", repo).stdout
        self.assertEqual(len(changed.splitlines()), 3)
        self.assertIn(b"D   empty.txt", changed)
        self.assertIn("initial files", self.run_tool("svn", "log", url).stdout.decode("utf8"))

    def test_distinct_files_merge_and_same_file_conflicts_against_fsfs(self):
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url = self.create(fs_type)
                wc = self.checkout(url, fs_type + "-seed")
                for name in ("a.txt", "b.txt"):
                    self.put(wc, name, b"base\n")
                    self.run_tool("svn", "add", wc / name)
                self.run_tool("svn", "commit", wc, "-m", "seed")
                left = self.checkout(url, fs_type + "-left")
                right = self.checkout(url, fs_type + "-right")
                self.put(left, "a.txt", b"left\n")
                self.put(right, "b.txt", b"right\n")
                self.run_tool("svn", "commit", left, "-m", "left")
                self.run_tool("svn", "commit", right, "-m", "right")
                self.assertEqual(self.youngest(repo), 3)
                self.assertEqual(self.run_tool("svn", "cat", url + "/a.txt").stdout, b"left\n")
                self.assertEqual(self.run_tool("svn", "cat", url + "/b.txt").stdout, b"right\n")
                self.run_tool("svn", "update", left)
                self.run_tool("svn", "update", right)
                self.put(left, "a.txt", b"winner\n")
                self.put(right, "a.txt", b"loser\n")
                self.run_tool("svn", "commit", left, "-m", "winner")
                self.run_tool("svn", "commit", right, "-m", "loser", ok=False)
                self.assertEqual(self.youngest(repo), 4)
                self.assertEqual(self.run_tool("svn", "cat", url + "/a.txt").stdout, b"winner\n")

    def test_parallel_publication_has_no_lost_nodes_or_revision_gaps(self):
        repo, url = self.create()
        def submit(number):
            return self.run_tool("svn", "mkdir", url + "/dir" + str(number), "-m", "parallel " + str(number))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(submit, range(12)))
        self.assertEqual(self.youngest(repo), 12)
        listing = self.run_tool("svn", "ls", url).stdout.decode("utf8").splitlines()
        self.assertEqual(set(listing), {"dir" + str(i) + "/" for i in range(12)})
        for rev in range(1, 13):
            self.assertEqual(len(self.run_tool("svnlook", "changed", "-r", rev, repo).stdout.splitlines()), 1)

    def test_case_sensitive_repository_names(self):
        names = {"Foo", "foo", "中文", "é", "e\u0301", "empty"}
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url = self.create(fs_type)
                self.run_tool("svn", "mkdir", *(url + "/" + quote(name, safe="") for name in sorted(names)),
                              "-m", "case and original Unicode forms")
                listing = ET.fromstring(self.run_tool("svn", "ls", "--xml", url).stdout)
                entries = listing.findall(".//entry")
                self.assertEqual({entry.findtext("name") for entry in entries}, names)
                self.assertTrue(all(entry.attrib["kind"] == "dir" for entry in entries))

    def test_revision_property_mutation_and_delete(self):
        repo, url = self.create()
        self.run_tool("svn", "mkdir", url + "/trunk", "-m", "r1")
        value = self.fixture / "revprop.bin"
        # svnadmin translates a local text file before the FS call. Raw binary
        # revision properties are tested directly through the native API probe.
        value.write_bytes(b"revision property\n")
        self.run_tool("svnadmin", "setrevprop", repo, "-r", "1", "test:binary", value)
        self.assertEqual(self.run_tool("svnlook", "propget", "--revprop", "-r", "1", repo, "test:binary").stdout, value.read_bytes())
        self.run_tool("svnadmin", "delrevprop", repo, "-r", "1", "test:binary")
        self.run_tool("svnlook", "propget", "--revprop", "-r", "1", repo, "test:binary", ok=False)
        self.assertEqual(self.youngest(repo), 1)

    def test_persistent_staging_checksum_rollback_abort_and_binary_api(self):
        repo, url = self.create()
        txn = self.run_tool("fs_probe", "begin", repo, "0").stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "mkdir", repo, txn, "stage")
        self.run_tool("fs_probe", "mkfile", repo, txn, "stage/file.bin")
        self.run_tool("fs_probe", "binary-nodeprop", repo, txn, "stage/file.bin")
        payload = self.fixture / "payload.bin"
        payload.write_bytes(bytes(range(256)) * 5000)
        self.run_tool("fs_probe", "write", repo, txn, "stage/file.bin", payload,
                      "11111111111111111111111111111111", ok=False)
        self.assertEqual(self.youngest(repo), 0)
        self.run_tool("fs_probe", "write", repo, txn, "stage/file.bin", payload,
                      hashlib.md5(payload.read_bytes()).hexdigest())
        self.run_tool("fs_probe", "abandon", repo, txn, "stage/file.bin")
        self.run_tool("fs_probe", "mkdir", repo, txn, "absent/child", ok=False)
        self.assertEqual(self.run_tool("svnlook", "cat", "-t", txn, repo, "stage/file.bin").stdout, payload.read_bytes())
        self.assertIn(txn.encode("ascii"), self.run_tool("svnadmin", "lstxns", repo).stdout)
        self.assertEqual(self.run_tool("fs_probe", "commit", repo, txn).stdout.strip(), b"1")
        self.assertEqual(self.run_tool("svn", "cat", url + "/stage/file.bin").stdout, payload.read_bytes())
        self.assertEqual(self.run_tool("svnlook", "propget", repo, "test:raw", "stage/file.bin").stdout, b"\0x\xff\0")
        self.run_tool("fs_probe", "binary-revprop", repo, "1")
        txn = self.run_tool("fs_probe", "begin", repo, "1").stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "mkdir", repo, txn, "aborted")
        self.run_tool("fs_probe", "abort", repo, txn)
        self.run_tool("fs_probe", "commit", repo, txn, ok=False)
        self.assertEqual(self.youngest(repo), 1)
        self.assertEqual(self.run_tool("svnadmin", "lstxns", repo).stdout, b"")
        self.assertNotIn(b"aborted", self.run_tool("svnlook", "tree", repo).stdout)

    def test_directory_overlay_compaction_and_empty_revision_roots(self):
        repo, url = self.create()
        txn = self.run_tool("fs_probe", "begin", repo, "0").stdout.decode("ascii").strip()
        for index in range(36):
            self.run_tool("fs_probe", "mkdir", repo, txn, "dir" + str(index))
        self.run_tool("fs_probe", "commit", repo, txn)
        self.assertEqual(len(self.run_tool("svn", "ls", url).stdout.splitlines()), 36)
        txn = self.run_tool("fs_probe", "begin", repo, "1").stdout.decode("ascii").strip()
        self.assertEqual(self.run_tool("fs_probe", "commit", repo, txn).stdout.strip(), b"2")
        self.assertEqual(self.youngest(repo), 2)
        self.assertEqual(self.run_tool("svnlook", "changed", repo).stdout, b"")
        self.assertEqual(self.run_tool("svn", "ls", url, "-r", "1").stdout,
                         self.run_tool("svn", "ls", url, "-r", "2").stdout)
        self.assertIn(b"2", self.run_tool("svnlook", "history", repo, "/").stdout)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeTreeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest(),
              "loader_sha256": hashlib.sha256((BIN / "libsvn_fs-1.dll").read_bytes()).hexdigest()}
    output = ROOT / "artifacts/tests/p1-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
