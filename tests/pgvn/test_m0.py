"""M0 native integration gate. Run from an elevated Windows development shell.

Creates only uniquely named temporary services and loopback URL reservations;
each is removed in finally. No installed SVN/Apache/PostgreSQL service is used.
"""
from pathlib import Path
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime/pgvn"
RESULTS = ROOT / "artifacts/pgvn/tests"
TEST_ID = "m0-" + uuid.uuid4().hex[:12]
WORK = RUNTIME / "tests" / TEST_ID
HIDDEN = subprocess.CREATE_NO_WINDOW
records = []


def command(args, expected=0, timeout=20):
    result = subprocess.run([str(a) for a in args], cwd=WORK, capture_output=True,
                            encoding="utf-8", errors="replace", creationflags=HIDDEN,
                            timeout=timeout)
    if result.returncode != expected:
        raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def request(port, path="/health/live", method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")
    finally:
        connection.close()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_live(port, process=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise AssertionError(f"Server exited early: {process.returncode}")
        try:
            status, _, body = request(port)
            if status == 200:
                return json.loads(body)
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    raise AssertionError("Listener did not become live")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def check_probe(port):
    status, headers, body = request(port)
    assert status == 200 and json.loads(body)["live"] is True
    assert headers.get("Cache-Control") == "no-store"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    status, _, body = request(port, "/health/ready")
    assert status == 503 and json.loads(body)["ready"] is False
    assert request(port, "/unknown")[0] == 404
    status, headers, _ = request(port, "/health/live", "POST", b"ignored")
    assert status == 405 and headers.get("Allow") == "GET"
    assert request(port, "/health/live", body=b"not accepted")[0] == 413
    status, _, body = request(port, "/monitor/")
    assert status == 200 and 'lang="zh-CN"' in body and "PgVN" in body


def parallel_requests(port, count=128):
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        statuses = list(executor.map(lambda _: request(port)[0], range(count)))
    assert statuses == [200] * count
    return {"requests": count, "clients": 16, "elapsed_seconds": time.monotonic() - start}


def build_identity():
    manifest = json.loads((ROOT / "artifacts/pgvn/build/manifest.json").read_text())
    for name, data in manifest["products"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == data["sha256"]
        assert "IMAGE_FILE_MACHINE_AMD64" in data["pe"]
    for name, symbol in (("mod_dav_pgvn.so", "dav_pgvn_module"),
                         ("mod_authz_pgvn.so", "authz_pgvn_module")):
        pe = manifest["products"]["runtime/pgvn/apache/modules/" + name]["pe"]
        assert "Name: " + symbol in pe
    diagnoses = {}
    for product in ("pgvnserve", "pgvnadmin"):
        diagnosis = json.loads(command([RUNTIME / "bin" / (product + ".exe"), "--version"]))
        assert diagnosis["milestone"] == manifest["milestone"] and diagnosis["svn_base"] == "1.14.5"
        assert diagnosis["apr_runtime"] == "1.7.6" and diagnosis["libpq_version"] == 180006
        assert diagnosis["ready"] is False
        assert diagnosis["process_code_page"] == 65001
        assert diagnosis["capabilities"]["svn_protocol"]==manifest["capabilities"]["svn_protocol"]
        assert diagnosis["capabilities"]["repository_by_id"] == manifest["capabilities"]["repository_by_id"]
        diagnoses[product] = diagnosis
    assert "1.14.5" in command([RUNTIME / "bin/svn.exe", "--version", "--quiet"])
    assert "Apache/2.4.68" in command([RUNTIME / "apache/bin/httpd.exe", "-v"])
    assert json.loads(command([RUNTIME/'apache/bin/pgvnhttpd.exe','--pgvn-runtime']))['process_code_page']==65001
    assert manifest["private_api_audit"]["ra_svn_exported"] == ["svn_ra_svn_init"]
    return {"configuration": manifest["configuration"], "products": diagnoses,
            "hashes": {k: v["sha256"] for k, v in manifest["products"].items()}}


def invalid_options():
    executable = RUNTIME / "bin/pgvnserve.exe"
    values = ("http://+:14691/", "http://0.0.0.0:14691/", "http://127.0.0.1:80/",
              "http://127.0.0.1:65536/", "http://127.0.0.1:14691/monitor/",
              "https://127.0.0.1:14691/", "http://127.0.0.1:999999999999/")
    for url in values:
        command([executable, "--monitor-url", url], expected=2)
    command([executable, "--unknown"], expected=2)
    command([RUNTIME / "bin/pgvnadmin.exe", "repo", "create"], expected=2)
    return {"invalid_urls_rejected": len(values), "unknown_commands_rejected": True}


def monitor_console():
    port = free_port()
    with (WORK / "console.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(RUNTIME / "bin/pgvnserve.exe"), "--monitor-url",
                                    f"http://127.0.0.1:{port}/"], cwd=WORK,
                                   stdout=log, stderr=log, creationflags=HIDDEN)
        try:
            wait_live(port, process)
            check_probe(port)
            concurrency = parallel_requests(port)
        finally:
            stop_process(process)
    # A new process must be able to claim the same URL after the former exits.
    with (WORK / "console-restart.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(RUNTIME / "bin/pgvnserve.exe"), "--monitor-url",
                                    f"http://127.0.0.1:{port}/"], cwd=WORK,
                                   stdout=log, stderr=log, creationflags=HIDDEN)
        try:
            wait_live(port, process)
        finally:
            stop_process(process)
    return {"probes": "live200/ready503/unknown404/write405/body413", "restart": True, **concurrency}


def apache_modules():
    manifest=json.loads((ROOT/'artifacts/pgvn/build/manifest.json').read_text())
    apache = RUNTIME / "apache"
    port = free_port()
    document_root = WORK / "htdocs"
    document_root.mkdir()
    config = WORK / "httpd.conf"
    config.write_text(f'''ServerRoot "{apache.as_posix()}"
Listen 127.0.0.1:{port}
ServerName 127.0.0.1
PidFile "{(WORK / 'httpd.pid').as_posix()}"
ErrorLog "{(WORK / 'apache-error.log').as_posix()}"
LogLevel warn
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule dav_module modules/mod_dav.so
LoadModule dav_pgvn_module modules/mod_dav_pgvn.so
LoadModule authz_pgvn_module modules/mod_authz_pgvn.so
DocumentRoot "{document_root.as_posix()}"
<Directory "{document_root.as_posix()}">
    Require all granted
</Directory>
<Location /dav-bootstrap>
    SetHandler pgvn-dav-bootstrap
</Location>
<Location /authz-bootstrap>
    SetHandler pgvn-authz-bootstrap
</Location>
ThreadsPerChild 8
AcceptFilter http none
''', encoding="utf-8")
    executable = apache / "bin/pgvnhttpd.exe"
    syntax = command([executable, "-f", config, "-t"])
    modules = command([executable, "-f", config, "-M"])
    assert "dav_pgvn_module (shared)" in modules and "authz_pgvn_module (shared)" in modules
    for cycle in range(2):
        with (WORK / f"apache-{cycle}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(executable), "-f", str(config), "-X"], cwd=WORK,
                                       stdout=log, stderr=log, creationflags=HIDDEN)
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise AssertionError("Apache exited before serving bootstrap modules")
                    try:
                        status, _, body = request(port, "/dav-bootstrap")
                        if status == 200:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    assert time.monotonic() < deadline, "Apache startup timeout"
                    time.sleep(0.1)
                assert json.loads(body)["capabilities"]["dav_protocol"]==manifest['capabilities']['dav_protocol']
                status, _, body = request(port, "/authz-bootstrap")
                assert status == 200 and json.loads(body)["product"] == "PgVN"
            finally:
                stop_process(process)
    return {"syntax": syntax.strip(), "loaded_modules": modules.strip(),
            "request_cycles": 2, "unload": "isolated Apache process exit"}


def service_state(name):
    output = command(["sc.exe", "query", name])
    # State numbers are stable across Windows language packs.
    import re
    match = re.search(r":\s+(\d+)\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING)", output)
    assert match, output
    return int(match.group(1))


def wait_service(name, expected):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if service_state(name) == expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"Service failed to enter state {expected}")


def local_service():
    name = "PgVN_M0_" + uuid.uuid4().hex[:12]
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    created = reserved = False
    cleanup = []
    measurements = {}
    try:
        # SID-based ACL avoids localized account names and grants listen only to LocalService.
        command(["netsh.exe", "http", "add", "urlacl", "url=" + url, "sddl=D:(A;;GX;;;LS)"])
        reserved = True
        binary = f'"{RUNTIME / "bin/pgvnserve.exe"}" --service --service-name {name} --monitor-url {url}'
        command(["sc.exe", "create", name, "binPath=", binary, "obj=", "NT AUTHORITY\\LocalService", "start=", "demand"])
        created = True
        configuration = command(["sc.exe", "qc", name])
        assert "LocalService" in configuration
        measurements["account"] = "NT AUTHORITY\\LocalService"
        for cycle in range(2):
            command(["sc.exe", "start", name])
            wait_service(name, 4)
            wait_live(port)
            check_probe(port)
            measurements[f"concurrency_{cycle}"] = parallel_requests(port)
            # Per-application HeaderWait applies only after the connection was routed.
            # Initial headers use the HTTP.sys global timer, which this test never changes.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/health/live")
                response = connection.getresponse()
                assert response.status == 200
                response.read()
                slow = connection.sock
                assert slow is not None, "Expected a reusable HTTP connection"
                slow.settimeout(30)
                slow.sendall(b"GET /health/live HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Incomplete:")
                start = time.monotonic()
                try:
                    data = slow.recv(1024)
                except ConnectionResetError:
                    data = b""
                elapsed = time.monotonic() - start
                assert not data or b"408" in data, data
                # The kernel does not promise millisecond-precise enforcement.
                # Querying the URL group at startup separately verifies the configured 5s value.
                assert 3 <= elapsed < 30, elapsed
                measurements[f"header_timeout_seconds_{cycle}"] = elapsed
            finally:
                connection.close()
            # Unrouted initial headers must not occupy the two application workers.
            initial_clients = []
            try:
                for _ in range(16):
                    client = socket.create_connection(("127.0.0.1", port), timeout=3)
                    initial_clients.append(client)
                    client.sendall(b"GET /health/live HTTP/1.1\r\n")
                assert request(port)[0] == 200
                start = time.monotonic()
                command(["sc.exe", "stop", name])
                wait_service(name, 1)
                measurements[f"stop_seconds_{cycle}"] = time.monotonic() - start
            finally:
                for client in initial_clients:
                    client.close()
        measurements["restart_cycles"] = 2
        measurements["first_header_timeout"] = "HTTP.sys global timer; unchanged by PgVN"
    finally:
        if created:
            try:
                if service_state(name) != 1:
                    command(["sc.exe", "stop", name])
                    wait_service(name, 1)
                command(["sc.exe", "delete", name])
                cleanup.append("temporary_service_deleted")
            except Exception as exc:
                cleanup.append(str(exc))
        if reserved:
            try:
                command(["netsh.exe", "http", "delete", "urlacl", "url=" + url])
                cleanup.append("temporary_urlacl_deleted")
            except Exception as exc:
                cleanup.append(str(exc))
        (WORK / "service-cleanup.json").write_text(json.dumps(cleanup, indent=2) + "\n")
        expected = int(created) + int(reserved)
        if len(cleanup) != expected or any("deleted" not in item for item in cleanup):
            raise AssertionError(f"Temporary service/URLACL cleanup failed: {cleanup}")
    measurements["cleanup"] = cleanup
    return measurements


def main():
    WORK.mkdir(parents=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    for name, check in (("native_build_identity", build_identity), ("invalid_options", invalid_options),
                        ("console_http", monitor_console), ("apache_module_load", apache_modules),
                        ("low_privilege_http_service", local_service)):
        start = time.monotonic()
        try:
            details = check()
            record = {"name": name, "status": "PASS", "details": details}
        except Exception:
            record = {"name": name, "status": "FAIL", "error": traceback.format_exc()}
        record["elapsed_seconds"] = time.monotonic() - start
        records.append(record)
        print(json.dumps(record), flush=True)
        if record["status"] == "FAIL":
            break
    passed = len(records) == 5 and all(r["status"] == "PASS" for r in records)
    report = {"test_id": TEST_ID, "milestone": "M0", "started_at": started_at,
              "status": "PASS" if passed else "FAIL", "records": records,
              "work_directory": str(WORK), "limits": "M0 checks build/bootstrap only; repository protocols are covered by their own milestone gates."}
    text = json.dumps(report, indent=2) + "\n"
    (RESULTS / (TEST_ID + ".json")).write_text(text, encoding="utf-8")
    (RESULTS / "m0-latest.json").write_text(text, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
