#!/usr/bin/env python3
"""Exercise the Linux monitor adapter the way a client would."""
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MONITOR = ROOT / "runtime/pgvn-linux/bin/pgvn-monitor"
INFO = ROOT / "runtime/pgvn-linux/bin/pgvn-platform-info"
WEB = ROOT / "web/pgvn-monitor"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(port, **extra):
    tokens = extra.pop("tokens")
    command = [str(MONITOR), "--bind", "127.0.0.1", "--port", str(port), "--web", str(WEB),
               "--tokens", str(tokens), "--timeout", "1", "--drain-ms", "600"]
    command.extend(extra.pop("args", []))
    process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        line = process.stderr.readline()
        if "listening" in line:
            return process
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read())
    raise RuntimeError("monitor did not listen")


def request(port, path, method="GET", headers=None, body=None):
    data = body.encode() if isinstance(body, str) else body
    call = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(call, timeout=2) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def main():
    header = (ROOT / "src/pgvn/core/pgvn.h").read_text()
    info = json.loads(subprocess.check_output([INFO], text=True))
    assert info["version"] in header and info["milestone"] in header and info["svn_base"] in header
    assert info["svn_protocol"] is False and info["credential_aead"] == "aes-256-gcm"
    limits = subprocess.check_output([MONITOR, "--limits"], text=True)
    assert "header_limit=32768" in limits and "queue_limit=64" in limits
    secret = "ab" * 32
    digest = hashlib.sha256(f"pgvn_{secret}".encode()).hexdigest()
    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"pgvn-http-{os.getpid()}"
    work.mkdir()
    tokens = work / "tokens"
    tokens.write_text(f"{digest} 120\n")
    port = free_port()
    while port < 1024:
        port = free_port()
    process = start(port, tokens=tokens)
    try:
        status, headers, body = request(port, "/health/live")
        assert status == 200 and body == b'{"live":true}\n', body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        status, headers, body = request(port, "/health/live", method="POST")
        assert status == 405 and headers["Allow"] == "GET"
        status, _, body = request(port, "/health/live", body="nope")
        assert status == 413 and b"request_body_not_supported" in body
        status, _, _ = request(port, "/no-such")
        assert status == 404
        status, headers, page = request(port, "/monitor/")
        assert status == 200 and page == (WEB / "index.html").read_bytes()
        status, headers, _ = request(port, "/metrics")
        assert status == 401 and "Bearer" in headers["WWW-Authenticate"]
        auth = {"Authorization": f"Bearer pgvn_{secret}", "Origin": "http://evil.example"}
        status, _, body = request(port, "/metrics", headers=auth)
        assert status == 403 and b"origin_denied" in body
        auth["Origin"] = f"http://127.0.0.1:{port}"
        status, _, body = request(port, "/metrics", headers=auth)
        assert status == 503 and b"database_unavailable" in body
        status, _, body = request(port, "/metrics?x=1", headers=auth)
        assert status == 400 and b"invalid_query" in body
        sock = socket.create_connection(("127.0.0.1", port), 2)
        sock.sendall(b"GET /health/live HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Big: " + b"A" * 40000 + b"\r\n\r\n")
        sock.settimeout(2)
        try:
            oversized = sock.recv(256)
        except socket.timeout:
            oversized = b""
        sock.close()
        assert b"200 OK" not in oversized
    finally:
        process.send_signal(subprocess.signal.SIGTERM)
        process.wait(timeout=5)
    port = free_port()
    while port < 1024:
        port = free_port()
    process = start(port, tokens=tokens, args=["--queue", "1", "--enable-test-hooks", "--timeout", "5", "--drain-ms", "700"])
    try:
        box = {}

        def hold():
            try:
                box["result"] = request(port, "/hold")
            except Exception as error:
                box["error"] = error

        thread = threading.Thread(target=hold)
        thread.start()
        held = False
        deadline = time.time() + 3
        while time.time() < deadline:
            if "holding" in process.stderr.readline():
                held = True
                break
        assert held
        status, _, body = request(port, "/health/live")
        assert status == 503 and b"queue_full" in body
        process.send_signal(subprocess.signal.SIGTERM)
        time.sleep(0.2)
        assert process.poll() is None
        thread.join(timeout=3)
        process.wait(timeout=3)
        assert process.returncode == 0
        assert "result" in box and box["result"][0] == 503
    finally:
        if process.poll() is None:
            process.kill()
    print("http checks passed")


if __name__ == "__main__":
    main()
