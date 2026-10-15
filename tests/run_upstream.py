"""Run pinned Apache command-line regression suites against PgFS or FSFS."""
from pathlib import Path
import hashlib
from collections import Counter
import json
import os
import re
import subprocess
import socket
import time
import sys
import uuid
from datetime import datetime, timezone
import ast
from compatibility_matrix import UPSTREAM_EXCLUSIONS

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/subversion-1.14.5/subversion/tests/cmdline"
BIN = ROOT / "runtime/svn/bin"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("run_upstream.py SUITE [--fs-type pg|fsfs] [test numbers or names]")
    suite = sys.argv[1]
    if not suite.replace("_", "").isalnum() or not (SOURCE / (suite + "_tests.py")).is_file():
        raise SystemExit("Unknown pinned upstream suite")
    fs_type = "pg"
    options = sys.argv[2:]
    transport = "file"
    if "--transport" in options:
        index = options.index("--transport")
        transport = options[index + 1]
        options = options[:index] + options[index + 2:]
    if transport not in ("file", "svn", "http"):
        raise SystemExit("Supported transports: file, svn, http")
    if "--fs-type" in options:
        index = options.index("--fs-type")
        fs_type = options[index + 1]
        options = options[:index] + options[index + 2:]
    if fs_type not in ("pg", "fsfs"):
        raise SystemExit("Unsupported regression backend")
    initial_digest = hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()
    applicable = "--applicable" in options
    if applicable:
        options.remove("--applicable")
    requested = list(options)
    exclusions = UPSTREAM_EXCLUSIONS.get(suite, {}) if applicable and fs_type == "pg" else {}
    if exclusions:
        if options:
            raise SystemExit("--applicable cannot be combined with a test selection")
        parsed = ast.parse((SOURCE / (suite + "_tests.py")).read_text(encoding="utf8"))
        tests = next(node.value for node in parsed.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "test_list" for target in node.targets))
        assert isinstance(tests, ast.List)
        options = [str(i) for i in range(1, len(tests.elts)) if i not in exclusions]
    parent = (ROOT / "runtime/upstream").resolve()
    directory = (parent / (suite + "-" + fs_type + "-" + uuid.uuid4().hex)).resolve()
    if not directory.is_relative_to(parent):
        raise SystemExit("Unsafe upstream test workspace")
    directory.mkdir(parents=True)
    suffix = "" if transport == "file" else "-" + transport
    log = ROOT / "artifacts/tests" / ("upstream-" + suite + "-" + fs_type + suffix + ".log")
    env = dict(os.environ, PGFS_DSN="host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable")
    command = [sys.executable, str(ROOT / "tests/upstream_entry.py"), suite, "--fs-type=" + fs_type,
               "--bin=" + str(BIN), "--tools-bin=" + str(BIN), "--srcdir=" + str(SOURCE),
               "--keep-local-tmp", *options]
    print("Running", suite, "against", fs_type, "in", directory, flush=True)
    server = None
    server_log = None
    try:
        if transport in ("svn", "http"):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            if transport == "http":
                import upstream_http
                server_command = upstream_http.command(directory, port, env)
                command.append("--httpd-version=2.4.68")
            else:
                server_command = [str(BIN / "svnserve.exe"), "--daemon", "--foreground",
                                  "--listen-host", "127.0.0.1", "--listen-port", str(port),
                                  "--root", str(directory)]
            server_log = (directory / (transport + "-server.log")).open("wb")
            server = subprocess.Popen(server_command, env=env, stdout=server_log,
                                      stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 15
            ready = False
            while time.monotonic() < deadline and server.poll() is None:
                with socket.socket() as check:
                    if check.connect_ex(("127.0.0.1", port)) == 0:
                        ready = True
                        break
                time.sleep(0.02)
            if not ready:
                raise RuntimeError("Upstream test " + transport + " server failed to start")
            command.append("--url=" + transport + "://127.0.0.1:" + str(port))
        with log.open("wb") as output:
            result = subprocess.run(command, cwd=directory, env=env, stdout=output, stderr=subprocess.STDOUT)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        if server_log is not None:
            server_log.close()
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "suite": suite, "fs_type": fs_type, "transport": transport,
              "exit_code": result.returncode, "workspace": str(directory), "log": str(log),
              "selection": options, "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    report.update(requested_selection=requested, applicable_matrix=applicable,
                  backend_specific_exclusions=exclusions)
    if report["dll_sha256"] != initial_digest:
        raise RuntimeError("PgFS module changed during this test run")
    report["source_sha256"] = hashlib.sha256((SOURCE / (suite + "_tests.py")).read_bytes()).hexdigest()
    log.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    output = log.read_text(encoding="utf8", errors="replace")
    print("Results:", dict(Counter(re.findall(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):", output, re.M))), flush=True)
    for line in output.splitlines():
        if line.startswith(("FAIL:", "XPASS:")):
            print(line, flush=True)
    print("Log:", log, flush=True)
    raise SystemExit(result.returncode)
