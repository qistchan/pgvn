"""Crash durability and cross-process writer lease tests through native APIs."""
from pathlib import Path
import configparser
import hashlib
import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timezone
from test_p1 import NativeTreeTests, ROOT, BIN
from test_p0 import sql, PG


class RecoveryTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest

    def stage(self, repo, base):
        txn = self.run_tool("fs_probe", "begin", repo, str(base)).stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "mkfile", repo, txn, "foo.bin")
        self.run_tool("fs_probe", "mkfile", repo, txn, "bar.bin")
        return txn

    def test_writer_lease_rejects_parallel_streams_and_dies_with_its_process(self):
        repo, url = self.create()
        txn = self.stage(repo, 0)
        marker, release = self.fixture / "writer-ready", self.fixture / "writer-release"
        payload = self.fixture / "input.bin"
        payload.write_bytes(b"completed\0binary\xff")
        with (self.fixture / "writer.log").open("wb") as output:
            writer = subprocess.Popen([str(BIN / "fs_probe.exe"), "hold-writer", str(repo), txn,
                                       "foo.bin", str(marker), str(release)], env=self.env,
                                      stdout=output, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and writer.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                for path in ("foo.bin", "bar.bin"):
                    error = self.run_tool("fs_probe", "write", repo, txn, path, payload, ok=False)
                    self.assertIn(b"already being written", error.stderr)
                error = self.run_tool("fs_probe", "commit", repo, txn, ok=False)
                self.assertIn(b"new_rev=-1", error.stderr)
                self.assertEqual(self.youngest(repo), 0)
                writer.terminate()
                writer.wait(timeout=10)
                config = configparser.ConfigParser()
                config.read(repo / "db/pgfs.conf")
                repository = config["pgfs"]["repository"]
                deadline = time.monotonic() + 5
                free = False
                while time.monotonic() < deadline:
                    free = sql("SELECT pg_try_advisory_lock(hashtextextended('" + repository + ":" + txn + "',0));") == b"t"
                    if free:
                        break
                    time.sleep(0.02)
                self.assertTrue(free, "Departed process still holds the writer lease")
                # Resume another file, proving abandoned state is not scoped
                # only to the original path. Partial foo bytes stay invisible.
                self.run_tool("fs_probe", "write", repo, txn, "bar.bin", payload)
                self.assertEqual(self.run_tool("svnlook", "cat", "-t", txn, repo, "foo.bin").stdout, b"")
                self.run_tool("fs_probe", "commit", repo, txn)
                self.assertEqual(self.youngest(repo), 1)
                self.assertEqual(self.run_tool("svnlook", "cat", repo, "bar.bin").stdout, payload.read_bytes())
                self.run_tool("svnadmin", "verify", repo)
            finally:
                if writer.poll() is None:
                    writer.terminate()
                    writer.wait(timeout=10)

    def test_immediate_database_stop_recovers_published_and_staged_binary_data(self):
        repo, url = self.create()
        committed = self.stage(repo, 0)
        payload = self.fixture / "committed.bin"
        payload.write_bytes(bytes(range(256)) * 6000)
        self.run_tool("fs_probe", "write", repo, committed, "foo.bin", payload)
        self.run_tool("fs_probe", "commit", repo, committed)
        staged = self.run_tool("fs_probe", "begin", repo, "1").stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "mkdir", repo, staged, "staged")
        self.run_tool("fs_probe", "mkfile", repo, staged, "staged/data.bin")
        staged_payload = self.fixture / "staged.bin"
        staged_payload.write_bytes(b"staged\0" + bytes(range(256)) * 5000)
        self.run_tool("fs_probe", "write", repo, staged, "staged/data.bin", staged_payload)
        expected = (ROOT / "runtime/pg-primary").resolve()
        actual = Path(sql("SHOW data_directory;").decode()).resolve()
        self.assertEqual(os.path.normcase(str(actual)), os.path.normcase(str(expected)))
        self.assertEqual(sql("SHOW fsync;"), b"on")
        self.assertEqual(sql("SHOW full_page_writes;"), b"on")
        with (self.fixture / "pg-control.log").open("wb") as output:
            stop = subprocess.run([str(PG / "bin/pg_ctl.exe"), "-D", str(expected), "-m", "immediate", "-w", "-t", "20", "stop"],
                                  stdout=output, stderr=subprocess.STDOUT, timeout=25)
            self.assertEqual(stop.returncode, 0)
            start = subprocess.run([str(PG / "bin/pg_ctl.exe"), "-D", str(expected), "-l", str(ROOT / "runtime/pg-primary.log"),
                                    "-o", "-p 15432 -h 127.0.0.1 -c shared_buffers=128MB -c max_connections=100", "-w", "-t", "30", "start"],
                                   stdout=output, stderr=subprocess.STDOUT, timeout=35)
            self.assertEqual(start.returncode, 0)
        self.assertEqual(self.youngest(repo), 1)
        self.assertEqual(self.run_tool("svnlook", "cat", repo, "foo.bin").stdout, payload.read_bytes())
        self.assertEqual(self.run_tool("svnlook", "cat", "-t", staged, repo, "staged/data.bin").stdout, staged_payload.read_bytes())
        self.assertIn(staged.encode("ascii"), self.run_tool("svnadmin", "lstxns", repo).stdout)
        self.assertEqual(self.run_tool("fs_probe", "commit", repo, staged).stdout.strip(), b"2")
        self.assertEqual(self.run_tool("svnlook", "cat", "-r", "1", repo, "foo.bin").stdout, payload.read_bytes())
        self.assertEqual(self.run_tool("svnlook", "cat", repo, "staged/data.bin").stdout, staged_payload.read_bytes())
        self.run_tool("svnadmin", "verify", repo)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    (ROOT / "artifacts/tests/recovery-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
