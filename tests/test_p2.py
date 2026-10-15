"""Copy, history, lock enforcement and merge gates, paired with FSFS."""
import json
import hashlib
import unittest
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from test_p1 import NativeTreeTests, ROOT, BIN


class CompatibilityTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest
    checkout = NativeTreeTests.checkout
    put = NativeTreeTests.put

    def seed(self, fs_type):
        repo, url = self.create(fs_type)
        wc = self.checkout(url, fs_type + "-seed")
        self.put(wc, "trunk/sub/file.txt", b"base\nline two\n")
        self.put(wc, "trunk/other.txt", b"other\n")
        (wc / "branches").mkdir()
        self.run_tool("svn", "add", wc / "trunk", wc / "branches")
        self.run_tool("svn", "commit", wc, "-m", "seed")
        self.run_tool("svn", "update", wc)
        return repo, url, wc

    def log(self, url, *options):
        output = self.run_tool("svn", "log", "--xml", "-v", url, *options).stdout
        document = ET.fromstring(output)
        return [(int(e.attrib["revision"]), sorted((p.text, tuple(sorted(p.attrib.items())))
                for p in e.findall("paths/path"))) for e in document.findall("logentry")]

    def test_copy_move_history_and_changes_match_fsfs(self):
        histories = {}
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url, wc = self.seed(fs_type)
                self.run_tool("svn", "copy", url + "/trunk", url + "/branches/one", "-m", "branch")
                branch = self.checkout(url + "/branches/one", fs_type + "-branch")
                self.put(branch, "sub/file.txt", b"changed\nline two\n")
                self.run_tool("svn", "commit", branch, "-m", "branch edit")
                self.run_tool("svn", "move", url + "/branches/one", url + "/branches/renamed", "-m", "move")
                path = url + "/branches/renamed/sub/file.txt"
                self.assertEqual(self.run_tool("svn", "cat", path + "@4", "-r", "1").stdout, b"base\nline two\n")
                self.assertEqual(self.run_tool("svn", "cat", path).stdout, b"changed\nline two\n")
                histories[fs_type] = self.log(path)
                self.assertEqual([r for r, _ in histories[fs_type]], [4, 3, 2, 1])
                self.assertEqual([r for r, _ in self.log(path, "--stop-on-copy")], [4])
                self.run_tool("svn", "blame", "--xml", path)
                # Copy + edit + delete + add in a single revision exercises
                # change folding relative to the copied source, not empty BASE.
                self.run_tool("svn", "update", wc)
                self.run_tool("svn", "copy", wc / "trunk", wc / "branches/combo")
                self.put(wc, "branches/combo/sub/file.txt", b"combo\n")
                self.run_tool("svn", "delete", wc / "branches/combo/other.txt")
                self.put(wc, "branches/combo/new.txt", b"new\n")
                self.run_tool("svn", "add", wc / "branches/combo/new.txt")
                self.run_tool("svn", "commit", wc, "-m", "copy and edit")
                histories[fs_type].extend(self.log(url, "-r", "5"))
                self.assertEqual(self.youngest(repo), 5)
        self.assertEqual(histories["pg"], histories["fsfs"])

    def test_lock_tokens_owners_steal_break_and_recursive_delete(self):
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url, wc = self.seed(fs_type)
                other = self.checkout(url, fs_type + "-other")
                target = "trunk/sub/file.txt"
                self.run_tool("svn", "lock", wc / target, "--username", "alice", "-m", "alice lock")
                info = ET.fromstring(self.run_tool("svn", "info", "--xml", url + "/" + target).stdout)
                self.assertEqual(info.findtext("entry/lock/owner"), "alice")
                self.put(other, target, b"without token\n")
                self.run_tool("svn", "commit", other, "--username", "bob", "-m", "wrong owner", ok=False)
                self.run_tool("svn", "commit", other, "--username", "alice", "-m", "missing token", ok=False)
                self.run_tool("svn", "delete", url + "/trunk/sub", "--username", "bob", "-m", "locked subtree", ok=False)
                self.assertEqual(self.youngest(repo), 1)
                self.put(wc, target, b"with token\n")
                self.run_tool("svn", "commit", wc, "--username", "alice", "-m", "token commit")
                self.assertEqual(self.youngest(repo), 2)
                info = ET.fromstring(self.run_tool("svn", "info", "--xml", url + "/" + target).stdout)
                self.assertIsNone(info.find("entry/lock"))
                self.run_tool("svn", "lock", wc / target, "--username", "alice")
                self.run_tool("svn", "revert", "--recursive", other)
                self.run_tool("svn", "update", other)
                self.run_tool("svn", "lock", other / target, "--username", "bob", ok=False)
                self.run_tool("svn", "lock", other / target, "--username", "bob", "--force")
                self.run_tool("svn", "unlock", wc / target, "--username", "alice", ok=False)
                self.run_tool("svn", "unlock", url + "/" + target, "--username", "alice", "--force")
                self.assertEqual(self.run_tool("svnadmin", "lslocks", repo).stdout, b"")
                # Exercise a real expiring FS lock using the unchanged upstream
                # public-API helper, then observe expiration through normal SVN.
                self.run_tool("lock-helper", repo, target, "alice", "3")
                info = ET.fromstring(self.run_tool("svn", "info", "--xml", url + "/" + target).stdout)
                self.assertIsNotNone(info.find("entry/lock/expires"))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    info = ET.fromstring(self.run_tool("svn", "info", "--xml", url + "/" + target).stdout)
                    if info.find("entry/lock") is None:
                        break
                    time.sleep(0.1)
                self.assertIsNone(info.find("entry/lock"), "Expired lock is still reported")
                self.assertEqual(self.run_tool("svnadmin", "lslocks", repo).stdout, b"")
                self.run_tool("svn", "delete", url + "/" + target, "--username", "bob",
                              "-m", "expired lock must not block a different user")
                self.assertEqual(self.youngest(repo), 3)
                self.run_tool("svnadmin", "verify", repo)

    def test_branch_merge_mergeinfo_and_merged_log_match_fsfs(self):
        outputs = {}
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url, wc = self.seed(fs_type)
                self.run_tool("svn", "copy", url + "/trunk", url + "/branches/one", "-m", "branch")
                self.put(wc, "trunk/sub/file.txt", b"trunk update\nline two\n")
                self.run_tool("svn", "commit", wc, "-m", "trunk edit")
                branch = self.checkout(url + "/branches/one", fs_type + "-merge")
                self.run_tool("svn", "merge", url + "/trunk", branch)
                self.assertEqual((branch / "sub/file.txt").read_bytes(), b"trunk update\nline two\n")
                self.run_tool("svn", "commit", branch, "-m", "merge trunk")
                self.assertEqual(self.youngest(repo), 4)
                outputs[fs_type] = self.run_tool("svn", "mergeinfo", "--show-revs", "merged",
                                               url + "/trunk", url + "/branches/one").stdout
                self.assertIn(b"r3", outputs[fs_type])
                self.run_tool("svn", "log", "-g", "--xml", url + "/branches/one")
        self.assertEqual(outputs["pg"], outputs["fsfs"])

    def test_native_node_relations_and_mutable_ids_match_fsfs(self):
        outcomes = {}
        for fs_type in ("pg", "fsfs"):
            with self.subTest(fs_type=fs_type):
                repo, url, wc = self.seed(fs_type)
                values = []
                for op, left, left_path, right, right_path in (
                    ("relation", "0", "/", "0", "/"),
                    ("relation", "0", "/", "1", "/"),
                    ("relation", "1", "trunk/sub/file.txt", "1", "trunk/sub/file.txt"),
                    ("relation-other-fs", "1", "trunk/sub/file.txt", "1", "trunk/sub/file.txt"),
                    ("relation-other-fs", "1", "absent", "1", "absent"),
                ):
                    values.append(self.run_tool("fs_probe", op, repo, left, left_path, right, right_path).stdout.strip())
                self.run_tool("fs_probe", "relation", repo, "1", "absent", "1", "trunk/sub/file.txt", ok=False)
                txn = self.run_tool("fs_probe", "begin", repo, "1").stdout.decode("ascii").strip()
                values.append(self.run_tool("fs_probe", "relation-txn", repo, txn, "/").stdout.strip())
                values.append(self.run_tool("fs_probe", "relation-txn", repo, txn, "trunk/sub/file.txt").stdout.strip())
                self.run_tool("fs_probe", "mkfile", repo, txn, "mutable.txt")
                self.run_tool("fs_probe", "stable-id", repo, txn, "mutable.txt")
                self.run_tool("fs_probe", "stable-id", repo, txn, "/")
                self.run_tool("fs_probe", "abort", repo, txn)
                outcomes[fs_type] = values
        self.assertEqual(outcomes["pg"], outcomes["fsfs"])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CompatibilityTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    (ROOT / "artifacts/tests/p2-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
