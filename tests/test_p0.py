"""Native loader/database persistence smoke test. Does not imply SVN compatibility."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "runtime/svn/bin"
PG = Path(os.environ.get("PGFS_POSTGRES_HOME", r"C:\Program Files\PostgreSQL\18"))


def native(program, *args, check=True):
    env = os.environ.copy()
    env["PGFS_DSN"] = "host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable"
    result = subprocess.run([str(BIN / program), *map(str, args)], env=env,
                            capture_output=True, timeout=30)
    if check and result.returncode:
        raise AssertionError(f"{program} failed: {result.stderr.decode('utf-8', 'replace')}")
    return result


def sql(statement):
    result = subprocess.run([str(PG / "bin/psql.exe"), "-X", "-w", "-A", "-t",
                             "-h", "127.0.0.1", "-p", "15432", "-U", "pgfs_test",
                             "-d", "pgfs_poc", "--set=ON_ERROR_STOP=1", "-c", statement],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.strip()


class NativeLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = ROOT / "runtime/repos"
        directory.mkdir(parents=True, exist_ok=True)
        cls.parent = Path(tempfile.mkdtemp(prefix="p0-", dir=directory))
        cls.repo = cls.parent / "repository"
        native("svnadmin.exe", "create", cls.repo, "--fs-type", "pg")
        cls.original_uuid = native("svnlook.exe", "uuid", cls.repo).stdout.strip().decode("ascii")

    def test_01_native_loader_creates_postgres_revision_zero(self):
        self.assertEqual((self.repo / "db/fs-type").read_text().strip(), "pg")
        self.assertEqual(native("svnlook.exe", "youngest", self.repo).stdout.strip(), b"0")
        count = sql("SELECT count(*) FROM pgfs.revisions WHERE "
                    f"repository_id='{self.original_uuid}' AND rev=0;")
        self.assertEqual(count, b"1")
        info = native("svnadmin.exe", "info", self.repo).stdout
        self.assertIn(b"Filesystem Type: pg", info)
        self.assertIn(b"Filesystem Format: 1", info)

    def test_02_revision_properties_are_read_from_postgres_without_text_loss(self):
        sql("INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) "
            f"VALUES('{self.original_uuid}',0,'pgfs:binary',decode('610062ff','hex')),"
            f"('{self.original_uuid}',0,'pgfs:empty',decode('','hex'));")
        binary = native("svnlook.exe", "propget", "--revprop", "-r", "0", self.repo,
                        "pgfs:binary").stdout
        self.assertEqual(binary, b"a\x00b\xff")
        empty = native("svnlook.exe", "propget", "--revprop", "-r", "0", self.repo,
                       "pgfs:empty").stdout
        self.assertEqual(empty, b"")
        missing = native("svnlook.exe", "propget", "--revprop", "-r", "0", self.repo,
                         "pgfs:missing", check=False)
        self.assertNotEqual(missing.returncode, 0)

    def test_03_uuid_change_survives_a_new_process(self):
        replacement = "12345678-1234-1234-1234-123456789abc"
        native("svnadmin.exe", "setuuid", self.repo, replacement)
        self.assertEqual(native("svnlook.exe", "uuid", self.repo).stdout.strip(), replacement.encode())
        native("svnadmin.exe", "setuuid", self.repo, self.original_uuid)

    def test_04_invalid_revision_is_rejected(self):
        result = native("svnlook.exe", "info", "-r", "1", self.repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"No such revision", result.stderr)

    def test_05_revision_root_is_immutable(self):
        result = native("fs_probe.exe", "revision-mkdir", self.repo, "0", "immutable", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"immutable", result.stderr.lower())
        self.assertEqual(native("svnlook.exe", "youngest", self.repo).stdout.strip(), b"0")

    def test_06_database_restart_preserves_revision_and_binary_properties(self):
        expected_directory = ROOT / "runtime/pg-primary"
        actual_directory = sql("SHOW data_directory;").decode()
        self.assertEqual(os.path.normcase(str(Path(actual_directory).resolve())),
                         os.path.normcase(str(expected_directory.resolve())))
        # Windows background descendants may inherit output handles. A log file
        # avoids waiting for pipe EOF after pg_ctl itself has already exited.
        control_log = self.parent / "pg-control.log"
        with control_log.open("wb") as log:
            result = subprocess.run([str(PG / "bin/pg_ctl.exe"), "-D", str(expected_directory),
                                     "-l", str(ROOT / "runtime/pg-primary.log"),
                                     "-o", "-p 15432 -h 127.0.0.1 -c shared_buffers=128MB -c max_connections=100",
                                     "-w", "-t", "30", "restart"], stdout=log,
                                    stderr=subprocess.STDOUT, timeout=45)
        self.assertEqual(result.returncode, 0, control_log.read_text(errors="replace"))
        self.assertEqual(native("svnlook.exe", "youngest", self.repo).stdout.strip(), b"0")
        self.assertEqual(native("svnlook.exe", "propget", "--revprop", "-r", "0", self.repo,
                                "pgfs:binary").stdout, b"a\x00b\xff")
        self.assertEqual(sql("SHOW fsync;"), b"on")
        self.assertEqual(sql("SHOW full_page_writes;"), b"on")

    def test_07_existing_fsfs_provider_still_loads(self):
        baseline = self.parent / "fsfs-control"
        native("svnadmin.exe", "create", baseline, "--fs-type", "fsfs")
        self.assertEqual(native("svnlook.exe", "youngest", baseline).stdout.strip(), b"0")
        self.assertEqual(native("svnlook.exe", "tree", baseline).stdout.strip(), b"/")

    @classmethod
    def tearDownClass(cls):
        output = ROOT / "artifacts/tests"
        output.mkdir(parents=True, exist_ok=True)
        (output / "p0-fixture.json").write_text(json.dumps({
            "repository": str(cls.repo), "repository_id": cls.original_uuid,
            "scope": "native loader, revision zero, binary revision properties and fail-closed writes"
        }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeLoaderTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"time_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "P0 native FS loading and persistent revision zero",
              "tests_run": result.testsRun,
              "failures": [case.id() for case, _ in result.failures],
              "errors": [case.id() for case, _ in result.errors],
              "passed": result.wasSuccessful(),
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest(),
              "loader_sha256": hashlib.sha256((BIN / "libsvn_fs-1.dll").read_bytes()).hexdigest()}
    output = ROOT / "artifacts/tests"
    output.mkdir(parents=True, exist_ok=True)
    (output / "p0-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
