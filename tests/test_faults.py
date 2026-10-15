"""Real PostgreSQL backend loss and dropped COMMIT response integration tests."""
import concurrent.futures
import configparser
import hashlib
import json
import socket
import socketserver
import struct
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from test_p1 import NativeTreeTests, ROOT, BIN
from test_p0 import sql


def receive(connection, size):
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise EOFError()
        result.extend(chunk)
    return bytes(result)


class PgProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        self.armed = False
        self.dropped = threading.Event()
        self.guard = threading.Lock()
        super().__init__(("127.0.0.1", 0), PgProxyHandler)


class PgProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection(("127.0.0.1", 15432), timeout=10)
        upstream.settimeout(15)
        self.request.settimeout(15)
        def server_to_client():
            try:
                while True:
                    header = receive(upstream, 5)
                    size = struct.unpack("!I", header[1:])[0]
                    if not 4 <= size <= 64 * 1024 * 1024:
                        raise ValueError("Invalid backend PostgreSQL frame")
                    payload = receive(upstream, size - 4)
                    with self.server.guard:
                        drop = self.server.armed and header[0:1] == b"C" and payload == b"COMMIT\0"
                        if drop:
                            self.server.armed = False
                            self.server.dropped.set()
                    if drop:
                        # CommandComplete(COMMIT) proves PostgreSQL committed;
                        # hide the response from libpq by breaking only this TCP
                        # connection. A new receipt connection remains usable.
                        self.request.shutdown(socket.SHUT_RDWR)
                        return
                    self.request.sendall(header + payload)
            except (OSError, EOFError, ValueError):
                pass
            finally:
                try:
                    self.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        transfer = threading.Thread(target=server_to_client, daemon=True)
        transfer.start()
        try:
            length = receive(self.request, 4)
            size = struct.unpack("!I", length)[0]
            upstream.sendall(length + receive(self.request, size - 4))
            while True:
                header = receive(self.request, 5)
                size = struct.unpack("!I", header[1:])[0]
                if not 4 <= size <= 64 * 1024 * 1024:
                    raise ValueError("Invalid frontend PostgreSQL frame")
                upstream.sendall(header + receive(self.request, size - 4))
        except (OSError, EOFError, ValueError):
            pass
        finally:
            upstream.close()
            transfer.join(timeout=1)


class FaultTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest

    def stage(self, repo):
        txn = self.run_tool("fs_probe", "begin", repo, "0").stdout.decode("ascii").strip()
        self.run_tool("fs_probe", "mkfile", repo, txn, "data.bin")
        payload = self.fixture / "input.bin"
        payload.write_bytes(bytes(range(256)) * 5000)
        self.run_tool("fs_probe", "write", repo, txn, "data.bin", payload)
        return txn, payload.read_bytes()

    def test_backend_termination_rolls_back_publication_and_allows_retry(self):
        application = "pgfs-fault-" + uuid.uuid4().hex
        self.env["PGFS_DSN"] += " application_name=" + application
        repo, _ = self.create()
        txn, payload = self.stage(repo)
        config = configparser.ConfigParser()
        config.read(repo / "db/pgfs.conf")
        repository = config["pgfs"]["repository"]
        function = "fault_" + uuid.uuid4().hex
        sql("CREATE FUNCTION pgfs." + function + "() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN "
            "IF NEW.repository_id='" + repository + "' AND NEW.rev>0 THEN PERFORM pg_sleep(10); END IF; "
            "RETURN NEW; END$$; CREATE TRIGGER " + function + " AFTER INSERT ON pgfs.revisions "
            "FOR EACH ROW EXECUTE FUNCTION pgfs." + function + "();")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                commit = executor.submit(self.run_tool, "fs_probe", "commit", repo, txn, ok=False)
                deadline = time.monotonic() + 10
                pid = b""
                while time.monotonic() < deadline:
                    pid = sql("SELECT pid FROM pg_stat_activity WHERE application_name='" + application +
                              "' AND wait_event='PgSleep' LIMIT 1;")
                    if pid:
                        break
                    time.sleep(0.02)
                self.assertTrue(pid, "Publication trigger was not reached")
                self.assertEqual(sql("SELECT pg_terminate_backend(" + pid.decode("ascii") + ");"), b"t")
                error = commit.result(timeout=15)
                self.assertIn(b"new_rev=-1", error.stderr)
            self.assertEqual(self.youngest(repo), 0)
            self.assertIn(txn.encode("ascii"), self.run_tool("svnadmin", "lstxns", repo).stdout)
            self.assertEqual(self.run_tool("svnlook", "cat", "-t", txn, repo, "data.bin").stdout, payload)
        finally:
            sql("DROP TRIGGER IF EXISTS " + function + " ON pgfs.revisions; DROP FUNCTION IF EXISTS pgfs." + function + "();")
        self.assertEqual(self.run_tool("fs_probe", "commit", repo, txn).stdout.strip(), b"1")
        self.assertEqual(self.youngest(repo), 1)
        self.assertEqual(self.run_tool("svnlook", "cat", repo, "data.bin").stdout, payload)
        self.run_tool("svnadmin", "verify", repo)

    def test_lost_commit_response_is_resolved_once_from_persistent_receipt(self):
        with PgProxy() as proxy:
            serving = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            serving.start()
            try:
                self.env["PGFS_DSN"] = self.env["PGFS_DSN"].replace("port=15432", "port=" + str(proxy.server_address[1]))
                repo, _ = self.create()
                txn, payload = self.stage(repo)
                with proxy.guard:
                    proxy.armed = True
                self.assertEqual(self.run_tool("fs_probe", "commit", repo, txn).stdout.strip(), b"1")
                self.assertTrue(proxy.dropped.is_set())
                self.assertEqual(self.youngest(repo), 1)
                self.assertEqual(self.run_tool("svnlook", "cat", repo, "data.bin").stdout, payload)
                self.assertEqual(self.run_tool("svnadmin", "lstxns", repo).stdout, b"")
                self.run_tool("fs_probe", "commit", repo, txn, ok=False)
                self.assertEqual(self.youngest(repo), 1)
                self.run_tool("svnadmin", "verify", repo)
            finally:
                proxy.shutdown()
                serving.join(timeout=2)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FaultTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    (ROOT / "artifacts/tests/faults-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
