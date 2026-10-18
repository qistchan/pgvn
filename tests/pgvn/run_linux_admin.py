"""Build, freeze and exercise the native Linux administrative CLI."""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
import platform
import pty
import re
import secrets as secret_factory
import shutil
import signal
import shlex
import socket
import subprocess
import struct
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pgvn"))
from build_metadata import collect, sha256
from embed_migrations import CATALOG, STORAGE

USAGE = 2
DATABASE = 3
SCHEMA = 4
DENIED = 5
MISSING = 6
CONFLICT = 7
SANITIZER_MARKERS = (
    "ERROR: AddressSanitizer",
    "ERROR: LeakSanitizer",
    "LeakSanitizer: detected memory leaks",
    "UndefinedBehaviorSanitizer",
    "runtime error:",
)


def scrub(value, secrets=()):
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r'("token"\s*:\s*")[^"]+(")', r"\1<redacted>\2", text)
    text = re.sub(r"\bpgvn_[0-9a-fA-F]{64}\b", "<redacted>", text)
    return text


def safe_arguments(arguments, secrets=()):
    output = []
    redact = False
    for argument in map(str, arguments):
        if redact:
            output.append("<redacted>")
            redact = False
        else:
            output.append(scrub(argument, secrets))
            redact = argument in ("--password", "--passphrase")
    return output


def run_suite(product, artifacts, configuration):
    product = Path(product)
    artifacts = Path(artifacts)
    manifest = json.loads((product / "build-manifest.json").read_text())
    frozen_inputs = json.loads((product / "generated/build-inputs.json").read_text())
    evidence_inputs = json.loads((artifacts / "inputs/build-metadata.json").read_text())
    if frozen_inputs != evidence_inputs:
        raise RuntimeError("Frozen generated build inputs differ from evidence")
    if frozen_inputs["build_identity"] != manifest["build_identity"]:
        raise RuntimeError("Frozen build identity differs from build manifest")
    if sha256(artifacts / "inputs/sdk-manifest.json") != manifest["sdk_manifest_sha256"]:
        raise RuntimeError("Frozen SDK manifest differs from build manifest")
    for relative, expected in manifest["files"].items():
        if sha256(product / relative) != expected:
            raise RuntimeError("Frozen product hash mismatch: " + relative)
    products = {item["name"]: item for item in manifest.get("products", [])}
    if set(products) != {"pgvnadmin", "pgvnserve"}:
        raise RuntimeError("Admin build must identify exactly pgvnadmin and pgvnserve")
    if not (product / "bin/pgvnadmin").is_file() or not (product / "bin/pgvnserve").is_file():
        raise RuntimeError("Admin product is missing one of its native binaries")
    server_runtime = json.loads(
        subprocess.check_output([product / "bin/pgvnserve", "--version"], text=True)
    )
    admin_runtime = json.loads(
        subprocess.check_output([product / "bin/pgvnadmin", "--version"], text=True)
    )
    if server_runtime != manifest["runtime"] or admin_runtime != manifest["admin_runtime"]:
        raise RuntimeError("Frozen executable identity differs from the build manifest")
    for name in ("PGVN_OWNER_DSN", "PGVN_ADMIN_DSN", "PGVN_SERVICE_DSN",
                 "PGVN_MONITOR_DSN", "PGVN_EMPTY_DSN"):
        if not os.environ.get(name):
            raise RuntimeError("Missing isolated connection environment: " + name)

    artifacts.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="admin-", dir=artifacts))
    environment = dict(os.environ)
    environment.pop("PGVN_DSN", None)
    environment.update({"LC_ALL": "C.UTF-8", "HOME": str(artifacts / "home")})
    Path(environment["HOME"]).mkdir(exist_ok=True)
    admin = product / "bin/pgvnadmin"
    server = product / "bin/pgvnserve"
    svn = product / "bin/svn"
    svnadmin = product / "bin/svnadmin"
    svnlook = product / "bin/svnlook"
    config_dir = temp_root / "svn-config"
    config_dir.mkdir()
    checks = []
    commands = []
    elapsed = {}
    secrets = []
    test_password = secret_factory.token_urlsafe(32)
    wrong_password = secret_factory.token_urlsafe(32)
    secrets.extend((test_password, wrong_password))
    owned_processes = []
    service_logs = []
    captured_processes = set()

    def command(arguments, timeout=60, expected=0, input_data=None, env=None,
                binary=False, sensitive_input=False, sensitive_output=False):
        arguments = list(map(str, arguments))
        if sensitive_input and input_data is not None:
            secrets.append(scrub(input_data, ()).strip())
        for index, argument in enumerate(arguments[:-1]):
            if argument in ("--password", "--passphrase") and arguments[index + 1] not in secrets:
                secrets.append(arguments[index + 1])
        try:
            result = subprocess.run(
                arguments,
                cwd=temp_root,
                env=env or environment,
                input=input_data,
                text=not binary,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            stdout = scrub(error.stdout or "", secrets)
            stderr = scrub(error.stderr or "", secrets)
            safe_args = safe_arguments(arguments, secrets)
            commands.append({
                "args": safe_args,
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "stdin": "redacted" if sensitive_input else ("present" if input_data is not None else None),
                "timed_out_after_seconds": timeout,
            })
            raise RuntimeError(
                "Command timed out after " + str(timeout) + " seconds: " +
                str(safe_args) + "\n" + stdout + stderr
            ) from None
        except OSError as error:
            safe_args = safe_arguments(arguments, secrets)
            message = scrub(error.strerror or "process launch failed", secrets)
            commands.append({
                "args": safe_args,
                "exit_code": None,
                "stdout": "",
                "stderr": message,
                "stdin": "redacted" if sensitive_input else ("present" if input_data is not None else None),
                "launch_error": error.errno,
            })
            raise RuntimeError(
                "Unable to launch command " + str(safe_args) + ": " + message
            ) from None
        if sensitive_output:
            match = re.search(r'"token"\s*:\s*"([^"]+)"', result.stdout)
            if match:
                secrets.append(match.group(1))
        stdout = scrub(result.stdout, secrets)
        stderr = scrub(result.stderr, secrets)
        commands.append({
            "args": safe_arguments(arguments, secrets),
            "exit_code": result.returncode,
            "stdout": {"length": len(result.stdout), "sha256": hashlib.sha256(result.stdout).hexdigest()}
            if binary else stdout,
            "stderr": stderr,
            "stdin": "redacted" if sensitive_input else ("present" if input_data is not None else None),
        })
        if expected is not None and result.returncode != expected:
            raise RuntimeError(
                "Command failed: " + str(safe_arguments(arguments, secrets)) + "\n" +
                stdout + stderr
            )
        return result

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    def admin_command(arguments, dsn=None, expected=0, timeout=60, input_data=None,
                      sensitive_input=False, sensitive_output=False, env_extra=None):
        env = dict(environment)
        if dsn is None:
            env.pop("PGVN_DSN", None)
        else:
            env["PGVN_DSN"] = dsn
        if env_extra:
            env.update(env_extra)
        return command(
            [admin, *arguments],
            timeout=timeout,
            expected=expected,
            input_data=input_data,
            env=env,
            sensitive_input=sensitive_input,
            sensitive_output=sensitive_output,
        )

    def svn_command(arguments, password=None, expected=0, timeout=60,
                    input_data=None, binary=False):
        if password is None:
            password = test_password
        if password not in secrets:
            secrets.append(password)
        return command(
            [svn, *arguments, "--non-interactive", "--no-auth-cache",
             "--config-dir", config_dir, "--username", "alice",
             "--password", password],
            timeout=timeout,
            expected=expected,
            input_data=input_data,
            binary=binary,
        )

    def write_config(path, port, dsn, key, capacity=64):
        path.write_text(
            "[pgvn]\nconnection = " + dsn +
            "\ncredential-key-file = " + str(key) +
            "\nidentity-domain = pgvn\nlisten-host = 127.0.0.1\nlisten-port = " +
            str(port) + "\nmax-connections = " + str(capacity) +
            "\nmax-request-bytes = 67108864\nmax-response-bytes = 67108864\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start_service(config, name, timeout=20):
        stdout_path = artifacts / (name + "-stdout.log")
        stderr_path = artifacts / (name + "-stderr.log")
        stdout = stdout_path.open("wb")
        stderr = stderr_path.open("wb")
        process = subprocess.Popen(
            [server, "--config", config, "--monitor-disabled"],
            cwd=temp_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        stdout.close()
        stderr.close()
        owned_processes.append(process)
        service_logs.append(stderr_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(name + " exited before readiness: " + str(process.returncode))
            if b"PgVN SVN listener ready" in stdout_path.read_bytes():
                return process
            time.sleep(0.05)
        raise TimeoutError(name + " did not become ready")

    def stop_service(process, signum=signal.SIGTERM, timeout=22):
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signum)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise TimeoutError("Owned pgvnserve process failed to stop")
        if code != 0:
            raise RuntimeError("Owned pgvnserve exited with status " + str(code))

    def spawn_idle(arguments, name):
        stdout_path = artifacts / (name + "-stdout.log")
        stderr_path = artifacts / (name + "-stderr.log")
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [admin, *arguments],
                cwd=temp_root,
                env=dict(environment, PGVN_DSN=os.environ["PGVN_ADMIN_DSN"]),
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
            )
        owned_processes.append(process)
        service_logs.append(stderr_path)
        proc = Path("/proc") / str(process.pid)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(name + " exited before reaching stdin")
            try:
                syscall = (proc / "syscall").read_text().split()
                stdin_target = os.readlink(proc / "fd/0")
                if (len(syscall) >= 4 and int(syscall[0], 16) == 7 and
                        int(syscall[2], 16) == 1 and int(syscall[3], 16) == 100 and
                        stdin_target.startswith("pipe:[")):
                    with (proc / "mem").open("rb", buffering=0) as memory:
                        memory.seek(int(syscall[1], 16))
                        stdin_fd = struct.unpack("=i", memory.read(4))[0]
                else:
                    stdin_fd = -1
            except (ValueError, OSError):
                stdin_fd = -1
            if stdin_fd == 0:
                commands.append({
                    "args": safe_arguments([admin, *arguments], secrets),
                    "stdin_wait_rendezvous": "poll syscall verified on stdin fd 0",
                })
                return process, stdout_path, stderr_path
            time.sleep(0.01)
        raise TimeoutError(name + " did not reach the stdin poll")

    def finish_idle(process, name, signum):
        process.send_signal(signum)
        process.wait(timeout=8)
        process.stdin.close()
        stdout = (artifacts / (name + "-stdout.log")).read_text(errors="replace")
        stderr = (artifacts / (name + "-stderr.log")).read_text(errors="replace")
        commands.append({
            "args": safe_arguments([admin, name, "<stdin>"], secrets),
            "exit_code": process.returncode,
            "stdout": scrub(stdout, secrets),
            "stderr": scrub(stderr, secrets),
            "stdin": "redacted",
        })
        return process.returncode, stderr

    def json_output(result):
        text = result.stdout.strip()
        return json.loads(text) if text else json.loads(result.stderr.strip())

    main_service = None
    try:
        timeout_marker = "admin-timeout-" + uuid.uuid4().hex
        try:
            command(
                [sys.executable, "-c",
                 "import sys,time; print(sys.argv[2],flush=True); time.sleep(10)",
                 "--password", timeout_marker],
                timeout=0.1,
                expected=None,
            )
        except RuntimeError as error:
            timeout_diagnostic = "".join(traceback.format_exception(error))
            check("command timeout diagnostics redact password argv and output",
                  timeout_marker not in timeout_diagnostic and
                  timeout_marker not in json.dumps(commands))
        else:
            raise AssertionError("Command timeout regression process did not time out")
        check("frozen admin product hashes", True)
        check("admin manifest runtime identity", json_output(
            admin_command(["--version"], dsn=None)
        ) == manifest["admin_runtime"])
        check("offline help", admin_command(["--help"], dsn=None).returncode == 0)
        diagnose = admin_command(["diagnose", "--json"], dsn=None)
        check("offline diagnose", diagnose.returncode == 0 and '"database":"not_configured"' in diagnose.stdout)
        invalid_config = admin_command(
            ["diagnose", "--config", temp_root / "missing.conf", "--json"],
            dsn=None, expected=USAGE
        )
        check("invalid configuration is structured usage JSON",
              invalid_config.returncode == USAGE and
              '"code":"invalid_arguments"' in invalid_config.stderr)
        empty_check = admin_command(["db", "check", "--json"],
                                    dsn=os.environ["PGVN_EMPTY_DSN"], expected=SCHEMA)
        check("blank database has schema error JSON", empty_check.returncode == SCHEMA and
              '"error"' in empty_check.stderr and '"schema_incompatible"' in empty_check.stderr)

        owner = os.environ["PGVN_OWNER_DSN"]
        admin_dsn = os.environ["PGVN_ADMIN_DSN"]
        service_dsn = os.environ["PGVN_SERVICE_DSN"]
        monitor_dsn = os.environ["PGVN_MONITOR_DSN"]
        admin_command(["db", "init"], dsn=owner)
        admin_command(["db", "migrate"], dsn=owner)
        admin_command(["db", "check"], dsn=owner)
        check("non-superuser init, migrate and check are idempotent", True)
        admin_command(["db", "grant", "--admin-role", "pgvn_admin",
                       "--service-role", "pgvn_service",
                       "--monitor-role", "pgvn_monitor"], dsn=owner)
        check("database grants provisioned by the CLI", True)
        config_dsn = temp_root / "dsn-override.conf"
        config_dsn.write_text("[pgvn]\nconnection = " + admin_dsn + "\n", encoding="utf-8")
        config_override = admin_command(
            ["db", "check", "--config", config_dsn],
            dsn=admin_dsn,
            env_extra={"PGVN_DSN": "\udcff"},
        )
        invalid_dsn = admin_command(
            ["db", "check", "--json"],
            dsn=None,
            expected=USAGE,
            env_extra={"PGVN_DSN": "\udcff"},
        )
        check("selected connection UTF-8 validation respects config precedence",
              config_override.returncode == 0 and invalid_dsn.returncode == USAGE and
              '"code":"invalid_arguments"' in invalid_dsn.stderr and
              "connection setting must be valid UTF-8" in invalid_dsn.stderr)

        key = temp_root / "credential.key"
        admin_command(["key", "create", key, "primary"], dsn=None)
        check("credential key is private", key.stat().st_mode & 0o777 == 0o600)
        check("key overwrite rejected", admin_command(
            ["key", "create", key, "replacement"], dsn=None, expected=None
        ).returncode != 0)
        link = temp_root / "credential-link"
        link.symlink_to(key)
        check("credential key symlink rejected", admin_command(
            ["key", "create", link, "symlink"], dsn=None, expected=None
        ).returncode != 0)
        second_key = temp_root / "credential-next.key"
        admin_command(["key", "create", second_key, "secondary"], dsn=None)
        admin_command(["user", "set", "pgvn", "alice", "--key-file", key],
                      dsn=admin_dsn, input_data=test_password + "\n", sensitive_input=True)
        user_list = admin_command(["user", "list", "pgvn"], dsn=admin_dsn)
        check("credential set and list", '"alice"' in user_list.stdout)
        def user_present(name):
            return '"' + name + '"' in admin_command(
                ["user", "list", "pgvn"], dsn=admin_dsn
            ).stdout

        failed_users = ("pty-user", "nul-user", "oversized-user")
        before_failed_inputs = user_list.stdout
        master, slave = pty.openpty()
        pty_process = subprocess.Popen(
            [admin, "user", "set", "pgvn", failed_users[0], "--key-file", key],
            cwd=temp_root, env=dict(environment, PGVN_DSN=admin_dsn),
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        os.close(slave)
        pty_stdout, pty_stderr = pty_process.communicate(timeout=5)
        os.close(master)
        commands.append({"args": safe_arguments([admin, "user", "set", "pgvn",
                         failed_users[0], "--key-file", key], secrets),
                         "exit_code": pty_process.returncode,
                         "stdout": scrub(pty_stdout, secrets), "stderr": scrub(pty_stderr, secrets),
                         "stdin": "interactive-pty"})
        nul_result = admin_command(
            ["user", "set", "pgvn", failed_users[1], "--key-file", key],
            dsn=admin_dsn, input_data="bad\x00password\n", sensitive_input=True, expected=None
        )
        oversized_result = admin_command(
            ["user", "set", "pgvn", failed_users[2], "--key-file", key],
            dsn=admin_dsn, input_data=("x" * 65536) + "\n",
            sensitive_input=True, expected=None
        )
        check("password input rejects PTY NUL and oversized data without row changes",
              pty_process.returncode != 0 and nul_result.returncode != 0 and
              oversized_result.returncode != 0 and
              all(not user_present(name) for name in failed_users) and
              '"alice"' in before_failed_inputs)
        crlf_user = "crlf-user"
        crlf_result = admin_command(
            ["user", "set", "pgvn", crlf_user, "--key-file", key],
            dsn=admin_dsn, input_data=test_password + "\r\n", sensitive_input=True
        )
        check("CRLF password input is accepted", crlf_result.returncode == 0 and user_present(crlf_user))
        admin_command(["user", "disable", "pgvn", crlf_user], dsn=admin_dsn)
        admin_command(["user", "rekey", "pgvn", "alice", "--key-file", second_key,
                       "--old-key-file", key], dsn=admin_dsn)
        check("credential key failure denied", admin_command(
            ["user", "rekey", "pgvn", "alice", "--key-file", key,
             "--old-key-file", temp_root / "missing.key"], dsn=admin_dsn, expected=None
        ).returncode != 0)

        created = json_output(admin_command(
            ["repo", "create", "demo", "--route", "/svn/demo"], dsn=admin_dsn
        ))
        repository_id = created["id"]
        check("repository created enabled", created["enabled"] is True)
        repo_list = json_output(admin_command(["repo", "list"], dsn=admin_dsn))
        repo_show = json_output(admin_command(["repo", "show", "name:demo"], dsn=admin_dsn))
        check("repository list and show", any(item["name"] == "demo" for item in repo_list["items"]) and
              repo_show["id"] == repository_id)
        missing_repo = admin_command(
            ["repo", "show", "name:missing", "--json"], dsn=admin_dsn, expected=MISSING
        )
        check("missing repository is structured not-found JSON",
              missing_repo.returncode == MISSING and '"code":"not_found"' in missing_repo.stderr)
        admin_command(["repo", "enable", "name:demo"], dsn=admin_dsn)
        admin_command(["repo", "disable", "name:demo"], dsn=admin_dsn)
        admin_command(["repo", "enable", "name:demo"], dsn=admin_dsn)
        check("repository enable and disable", True)

        authz = temp_root / "demo.authz"
        authz.write_text("[demo:/]\n@admins = rw\n* =\n", encoding="utf-8")
        groups = temp_root / "groups.conf"
        groups.write_text("[groups]\nadmins = alice\n", encoding="utf-8")
        admin_command(["authz", "validate", "--authz", authz, "--groups", groups], dsn=None)
        current = json_output(admin_command(["config", "show", "name:demo"], dsn=admin_dsn))
        generation = int(current["generation"])
        admin_command(["config", "publish", "name:demo", "--expected-generation",
                       generation, "--authz", authz, "--groups", groups], dsn=admin_dsn)
        stale = admin_command(["config", "publish", "name:demo", "--expected-generation",
                               generation, "--authz", authz, "--groups", groups], dsn=admin_dsn,
                               expected=CONFLICT)
        check("configuration publish and stale generation conflict",
              stale.returncode == CONFLICT)
        authz_result = json_output(admin_command(
            ["authz", "check", "name:demo", "alice", "/", "write"], dsn=admin_dsn
        ))
        check("authz check after CLI publication", authz_result["allowed"] is True)

        service_look = admin_command(["look", "youngest", "name:demo"], dsn=service_dsn)
        service_mutation = admin_command(["repo", "disable", "name:demo"], dsn=service_dsn,
                                         expected=DENIED)
        monitor_check = admin_command(["db", "check"], dsn=monitor_dsn, expected=None)
        check("service and monitor lookups are restricted to allowed reads",
              service_look.returncode == 0 and service_mutation.returncode == DENIED and
              monitor_check.returncode == 0)

        standard_repo = temp_root / "standard-repo"
        command([svnadmin, "create", standard_repo])
        standard_url = "file://" + str(standard_repo)
        standard_wc = temp_root / "standard-wc"
        command([svn, "checkout", standard_url, standard_wc])
        (standard_wc / "binary-\u2603.bin").write_bytes(b"\x00\x01\xff\n")
        (standard_wc / "unicode.txt").write_text("P\u00e4iv\u00e4 \u96ea\n", encoding="utf-8")
        command([svn, "add", standard_wc / "binary-\u2603.bin", standard_wc / "unicode.txt"])
        command([svn, "commit", standard_wc, "-m", "admin round trip"])
        original_dump = command([svnadmin, "dump", standard_repo], binary=True).stdout
        dump_file = temp_root / "standard.dump"
        dump_file.write_bytes(original_dump)
        imported = json_output(admin_command(
            ["repo", "create", "imported", "--route", "/svn/imported"], dsn=admin_dsn
        ))
        admin_command(["repo", "disable", "name:imported"], dsn=admin_dsn)
        imported_show = json_output(admin_command(
            ["repo", "show", "name:imported"], dsn=admin_dsn
        ))
        check("history destination disabled before load", imported_show["enabled"] is False)
        admin_command(["load", "name:imported", "--file", dump_file], dsn=admin_dsn)
        admin_command(["verify", "name:imported"], dsn=admin_dsn)
        youngest = admin_command(["look", "youngest", "name:imported"], dsn=admin_dsn)
        cat = command([admin, "look", "cat", "name:imported", "/binary-\u2603.bin"],
                      env=dict(environment, PGVN_DSN=admin_dsn), binary=True)
        roundtrip_dump = temp_root / "roundtrip.dump"
        admin_command(["dump", "name:imported", "--file", roundtrip_dump], dsn=admin_dsn)
        roundtrip_repo = temp_root / "roundtrip-repo"
        command([svnadmin, "create", roundtrip_repo])
        with roundtrip_dump.open("rb") as stream:
            command([svnadmin, "load", roundtrip_repo], input_data=stream.read(),
                    binary=True, sensitive_input=False)
        check("binary and Unicode history load verify look cat dump round trip",
              youngest.stdout.strip() == "1" and cat.stdout == b"\x00\x01\xff\n" and
              roundtrip_dump.read_bytes() == original_dump)

        token_created = json_output(admin_command(
            ["monitor-token", "create", "--label", "admin-test", "--expires-in", "86400"],
            dsn=admin_dsn, sensitive_output=True
        ))
        token = token_created["token"]
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_request = artifacts / "token-check.request"
        token_result = artifacts / "token-check.result"
        token_request.write_text(token_created["id"], encoding="ascii")
        deadline = time.monotonic() + 15
        while not token_result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not token_result.exists():
            raise TimeoutError("Token database hash query did not complete")
        check("monitor token stores SHA-256 only", token_result.read_text().strip() == token_digest)
        token_list = json_output(admin_command(["monitor-token", "list"], dsn=admin_dsn))
        listed_token = next((item for item in token_list["items"]
                             if item["id"] == token_created["id"]), {})
        admin_command(["monitor-token", "expire", token_created["id"], "--expires-in", "0"],
                      dsn=admin_dsn)
        expired_tokens = json_output(admin_command(["monitor-token", "list"], dsn=admin_dsn))
        expired_token = next((item for item in expired_tokens["items"]
                              if item["id"] == token_created["id"]), {})
        admin_command(["monitor-token", "revoke", token_created["id"]], dsn=admin_dsn)
        revoked_tokens = json_output(admin_command(["monitor-token", "list"], dsn=admin_dsn))
        revoked_token = next((item for item in revoked_tokens["items"]
                              if item["id"] == token_created["id"]), {})
        check("monitor token create list expiry revoke with hash only",
              token not in str(token_list) and listed_token.get("label") == "admin-test" and
              expired_token.get("expired") is True and revoked_token.get("revoked_at") is not None)

        backup = json_output(admin_command(
            ["maintenance", "hotcopy", "name:imported", "backup", "--route", "/backup"],
            dsn=admin_dsn
        ))
        admin_command(["verify", "name:backup"], dsn=admin_dsn)
        admin_command(["maintenance", "purge", "name:backup",
                       "--confirm-id", backup["id"]], dsn=admin_dsn)
        check("maintenance hotcopy verify and confirmed purge", True)

        port = free_port()
        service_config = temp_root / "service.conf"
        write_config(service_config, port, os.environ["PGVN_SERVICE_DSN"], second_key)
        main_service = start_service(service_config, "admin-service")
        url = "svn://127.0.0.1:" + str(port) + "/svn/demo"
        info = svn_command(["info", url])
        wrong = svn_command(["info", url], password=wrong_password, expected=None)
        admin_command(["user", "disable", "pgvn", "alice"], dsn=admin_dsn)
        disabled = svn_command(["info", url], expected=None)
        stop_service(main_service)
        main_service = None
        admin_command(["user", "set", "pgvn", "alice", "--key-file", key],
                      dsn=admin_dsn, input_data=test_password + "\n", sensitive_input=True)
        write_config(service_config, port, os.environ["PGVN_SERVICE_DSN"], key)
        main_service = start_service(service_config, "admin-service-restored")
        restored = svn_command(["info", url])
        check("rekey authentication, disable denial, and credential restoration",
              info.returncode == 0 and disabled.returncode != 0 and restored.returncode == 0 and
              wrong.returncode != 0 and "Authentication" in wrong.stderr)
        checkout = temp_root / "checkout"
        svn_command(["checkout", url, checkout])
        (checkout / "cli.bin").write_bytes(b"CLI-created repository \x00\xff\n")
        svn_command(["add", checkout / "cli.bin"])
        svn_command(["commit", checkout, "-m", "admin CLI service commit"])
        service_cat = svn_command(["cat", url + "/cli.bin"], binary=True)
        check("authenticated checkout commit and cat", service_cat.stdout == b"CLI-created repository \x00\xff\n")
        def handshake_and_disconnect(_):
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.settimeout(3)
                return bool(client.recv(1))

        handshakes = []
        for _ in range(3):
            with ThreadPoolExecutor(max_workers=16) as executor:
                handshakes = list(executor.map(handshake_and_disconnect, range(16)))
            if not all(handshakes):
                raise RuntimeError("Concurrent SVN handshake was not accepted")
        info_after_disconnects = svn_command(["info", url])
        check("concurrent SVN disconnect and slot reuse followed by info",
              len(handshakes) == 16 and info_after_disconnects.returncode == 0)
        stop_service(main_service)
        main_service = None

        idle_user, _, _ = spawn_idle(["user", "set", "pgvn", "idle-user", "--key-file", key],
                                     "idle-password")
        password_code, password_stderr = finish_idle(idle_user, "idle-password", signal.SIGINT)
        check("SIGINT cancels while waiting for redirected password", password_code != 0 and
              "cancel" in password_stderr.lower())
        idle_load, _, _ = spawn_idle(["load", "name:imported"], "idle-load")
        load_code, load_stderr = finish_idle(idle_load, "idle-load", signal.SIGTERM)
        check("SIGTERM cancels while waiting for load stdin", load_code != 0 and
              "cancel" in load_stderr.lower())

        lock_request = artifacts / "lock-holder.request"
        lock_held = artifacts / "lock-holder.held"
        lock_released = artifacts / "lock-holder.released"
        lock_wait = artifacts / "lock-wait.request"
        lock_blocked = artifacts / "lock-wait.blocked"
        lock_done = artifacts / "lock-wait.done"
        lock_session_clean = artifacts / "lock-wait.session-clean"
        for path in (lock_request, lock_held, lock_released, lock_wait, lock_blocked,
                     lock_done, lock_session_clean):
            path.unlink(missing_ok=True)
        lock_request.write_text("pgfs.transactions", encoding="ascii")
        deadline = time.monotonic() + 15
        while not lock_held.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not lock_held.exists():
            raise TimeoutError("Lock holder rendezvous did not complete")
        lock_wait.write_text("look transactions", encoding="ascii")
        waiter = subprocess.Popen(
            [admin, "look", "transactions", "name:imported"],
            cwd=temp_root,
            env=dict(environment, PGVN_DSN=admin_dsn),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        owned_processes.append(waiter)
        deadline = time.monotonic() + 15
        while not lock_blocked.exists() and time.monotonic() < deadline:
            if waiter.poll() is not None:
                raise RuntimeError("Lock-wait admin command completed before rendezvous")
            time.sleep(0.05)
        if not lock_blocked.exists():
            raise TimeoutError("Admin command did not reach the PostgreSQL lock wait")
        cancelled_at = time.monotonic()
        waiter.send_signal(signal.SIGINT)
        waiter_stdout, waiter_stderr = waiter.communicate(timeout=8)
        captured_processes.add(id(waiter))
        elapsed["blocked_sql_statement_timeout_after_sigint_seconds"] = time.monotonic() - cancelled_at
        commands.append({"args": safe_arguments([admin, "look", "transactions", "name:imported"], secrets),
                         "exit_code": waiter.returncode, "stdout": scrub(waiter_stdout, secrets),
                         "stderr": scrub(waiter_stderr, secrets), "stdin": None})
        lock_done.write_text("waiter-exited\n", encoding="ascii")
        deadline = time.monotonic() + 10
        while not lock_session_clean.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not lock_session_clean.exists():
            raise TimeoutError("PgVN lock/session cleanup was not observed")
        release_wait_started = time.monotonic()
        deadline = time.monotonic() + 12
        while not lock_released.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        elapsed["lock_holder_release_seconds"] = time.monotonic() - release_wait_started
        check("blocked SQL exits at statement timeout after SIGINT (not active query cancellation)",
              waiter.returncode != 0 and "SQLSTATE 57014" in waiter_stderr and
              elapsed["blocked_sql_statement_timeout_after_sigint_seconds"] < 8 and
              lock_session_clean.read_text().strip() == "0" and lock_released.exists() and
              elapsed["lock_holder_release_seconds"] < 12)

        freeze_child = temp_root / "freeze-child-ran"
        freeze_env = dict(environment)
        freeze_env.pop("PGVN_DSN", None)
        freeze = command([admin, "maintenance", "freeze", "name:imported",
                          "--timeout", "1", "--", "sh", "-c",
                          "touch " + str(freeze_child)],
                         expected=USAGE, env=freeze_env)
        check("freeze is rejected before DSN or child execution",
              freeze.returncode == USAGE and not freeze_child.exists())
        invalid_utf8 = subprocess.run(
            [os.fsencode(admin), b"--\xff"],
            cwd=temp_root,
            env=environment,
            capture_output=True,
        )
        check("invalid UTF-8 argv is rejected",
              invalid_utf8.returncode == USAGE and b"UTF-8" in invalid_utf8.stderr)
    finally:
        cleanup_errors = []
        for process in owned_processes:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception as error:
                        cleanup_errors.append("process cleanup: " + str(error))
                except Exception as error:
                    cleanup_errors.append("process cleanup: " + str(error))
            if id(process) not in captured_processes and (process.stdout is not None or process.stderr is not None):
                try:
                    stdout, stderr = process.communicate(timeout=2)
                    commands.append({
                        "args": ["<owned child>"],
                        "exit_code": process.returncode,
                        "stdout": scrub(stdout, secrets),
                        "stderr": scrub(stderr, secrets),
                        "stdin": None,
                    })
                    captured_processes.add(id(process))
                except Exception as error:
                    cleanup_errors.append("process output capture: " + str(error))
        diagnostics = []
        for path in service_logs:
            if path.exists():
                diagnostics.append((str(path), path.read_text(errors="replace")))
        diagnostics.extend((str(item.get("args", "<command>")), item.get("stderr", ""))
                           for item in commands)
        sanitizer_diagnostics = [
            name + ": " + marker
            for name, text in diagnostics
            for marker in SANITIZER_MARKERS if marker in text
        ]
        cleanup_errors.extend(sanitizer_diagnostics)
        (artifacts / "admin-commands.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in commands),
            encoding="utf-8",
        )
        shutil.rmtree(temp_root, ignore_errors=True)
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
    if collect(ROOT, "linux", configuration) != frozen_inputs:
        raise RuntimeError("Source/build metadata changed during the admin suite")
    return {
        "checks": checks,
        "elapsed_seconds": elapsed,
        "runtime": {"server": server_runtime, "admin": admin_runtime},
        "sanitizer_diagnostics": sanitizer_diagnostics,
        "limitations": [
            "This suite validates the native Linux administrative CLI and its supporting "
            "foreground svn:// listener; monitoring, Apache/DAV, packaging and deployment "
            "remain outside scope.",
            "Linux maintenance freeze is intentionally rejected before database access because "
            "contained child execution is not implemented.",
            "SIGINT does not actively cancel synchronous PQexecParams; blocked SQL exits at "
            "PostgreSQL statement_timeout with SQLSTATE 57014.",
            "Telemetry-enabled operation was not validated; this run exercises the disabled "
            "telemetry build configuration.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--configuration", choices=("Release", "Debug"), default="Release")
    parser.add_argument("--sanitize", action="store_true")
    parser.add_argument("--builder-image", default="pgvn-linux-builder:24.04")
    parser.add_argument("--postgres-image", default="postgres:18.6")
    parser.add_argument("--in-container", action="store_true")
    parser.add_argument("--product", type=Path)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    if args.in_container:
        report_path = args.artifacts / "admin-suite-report.json"
        try:
            result = run_suite(args.product.resolve(strict=True), args.artifacts.resolve(strict=True),
                               args.configuration)
            report = {
                "format": 1,
                "status": "PASS",
                "scope": "Native Linux pgvnadmin with supporting foreground svn:// service",
                "checks": result["checks"],
                "elapsed_seconds": result["elapsed_seconds"],
                "runtime": result["runtime"],
                "sanitizer_diagnostics": result["sanitizer_diagnostics"],
                "limitations": result["limitations"],
            }
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"report": str(report_path), "checks": len(result["checks"]),
                              "status": "PASS"}), flush=True)
            return 0
        except Exception:
            error = traceback.format_exc()
            (args.artifacts / "admin-suite-error.log").write_text(error, encoding="utf-8")
            report = {
                "format": 1,
                "status": "FAIL",
                "scope": "Native Linux pgvnadmin with supporting foreground svn:// service",
                "checks": [],
                "errors": [error],
            }
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(error, file=sys.stderr, flush=True)
            return 1

    run_id = "linux-admin-" + uuid.uuid4().hex[:12]
    test_name = run_id + "-suite"
    artifacts = ROOT / "artifacts/pgvn/linux-admin" / run_id
    artifacts.mkdir(parents=True)
    environment = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    commands = []
    errors = []
    db_started = False
    test_started = False
    builder_os = ""
    host_os = Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else platform.platform()
    db_name = run_id

    def outer_command(arguments, timeout=1800, data=None, expected=0):
        result = subprocess.run(
            list(map(str, arguments)),
            cwd=ROOT,
            env=environment,
            input=data,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        commands.append({
            "args": safe_arguments(arguments),
            "exit_code": result.returncode,
            "stdout": scrub(result.stdout),
            "stderr": scrub(result.stderr),
            "stdin": "present" if data is not None else None,
        })
        if expected is not None and result.returncode != expected:
            raise RuntimeError("Command failed: " + str(safe_arguments(arguments)) +
                               "\n" + scrub(result.stdout) + scrub(result.stderr))
        return result

    def db_exec(arguments, timeout=60, data=None, expected=0):
        return outer_command(docker + ["exec", "-i", db_name, *arguments],
                             timeout=timeout, data=data, expected=expected)

    try:
        sdk = args.sdk.resolve(strict=True)
        image_ids = {}
        for image in (args.builder_image, args.postgres_image):
            image_ids[image] = outer_command(
                docker + ["image", "inspect", "--format", "{{.Id}}", image]
            ).stdout.strip()
        builder_os = outer_command(
            docker + ["run", "--rm", "--network", "none", args.builder_image,
                      "sh", "-c", "cat /etc/os-release"]
        ).stdout
        build_args = [
            "python3", "scripts/pgvn/build.py", "--target", "linux",
            "--components", "admin", "--configuration", args.configuration,
            "--linux-sdk", sdk,
        ]
        if args.sanitize:
            build_args.append("--sanitize")
        build_docker = [
            *docker, "run", "--rm", "--network", "none",
            "--user", str(os.getuid()) + ":" + str(os.getgid()),
            "--memory", "3g", "--cpus", "3",
            "--mount", "type=bind,src=" + str(ROOT) + ",dst=" + str(ROOT),
            "-w", ROOT, args.builder_image, *build_args,
        ]
        built = outer_command(build_docker, timeout=2400)
        (artifacts / "build.log").write_text(built.stdout + built.stderr, encoding="utf-8")
        build_result = json.loads(built.stdout.strip().splitlines()[-1])
        work = Path(build_result["output"])
        manifest = json.loads((work / "build-manifest.json").read_text())
        metadata = collect(ROOT, "linux", args.configuration)
        generated = json.loads((work / "generated/build-inputs.json").read_text())
        if generated != metadata or manifest["build_identity"] != metadata["build_identity"]:
            raise RuntimeError("Generated metadata does not match the build manifest")
        sdk_manifest_source = sdk / "sdk-manifest.json"
        sdk_manifest_hash = sha256(sdk_manifest_source)
        if sdk_manifest_hash != manifest["sdk_manifest_sha256"]:
            raise RuntimeError("SDK manifest differs from build input")
        product = artifacts / "frozen-product"
        shutil.copytree(work, product, ignore=shutil.ignore_patterns("*.o", "commands.jsonl"))
        if any(sha256(product / relative) != expected
               for relative, expected in manifest["files"].items()):
            raise RuntimeError("Frozen admin product hash mismatch")
        (artifacts / "inputs").mkdir()
        shutil.copytree(product / "generated", artifacts / "inputs/generated")
        (artifacts / "inputs/build-metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        shutil.copy2(sdk_manifest_source, artifacts / "inputs/sdk-manifest.json")
        if sha256(artifacts / "inputs/sdk-manifest.json") != manifest["sdk_manifest_sha256"]:
            raise RuntimeError("Copied SDK manifest hash mismatch")
        for relative, expected in metadata["sources"].items():
            source = ROOT / relative
            target = artifacts / "inputs/source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target) != expected:
                raise RuntimeError("Source changed while freezing: " + relative)
        for directory, names, prefix in (
            (ROOT / "sql", STORAGE, "storage"),
            (ROOT / "sql/pgvn", CATALOG, "catalog"),
        ):
            for name in names:
                source = directory / name
                target = artifacts / "inputs/migrations" / prefix / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha256(target) != sha256(source):
                    raise RuntimeError("Migration changed while freezing: " + name)
        upstream = manifest["upstream_inputs"]
        upstream_source = sdk / "sources/subversion/subversion"
        for name, expected in upstream["source_files"].items():
            relative = name.split("/subversion/", 1)[1]
            source = upstream_source / relative
            target = artifacts / "inputs/upstream-source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target) != expected:
                raise RuntimeError("Subversion input changed while freezing: " + relative)
        private = upstream_source / "svn_private_config.h"
        private_copy = artifacts / "inputs/upstream-source/svn_private_config.h"
        shutil.copy2(private, private_copy)
        if sha256(private_copy) != upstream["generated_private_config_sha256"]:
            raise RuntimeError("Generated Subversion config changed while freezing")
        for name in ("repos.json", "protocol.json"):
            source = ROOT / "patches/pgvn/subversion-1.14.5" / name
            target = artifacts / "inputs/patches" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            expected = upstream["repository_patch_sha256"] if name == "repos.json" else upstream["protocol_patch_sha256"]
            if sha256(target) != expected:
                raise RuntimeError("Copied patch hash mismatch: " + name)
        (artifacts / "inputs/build-manifest.json").write_bytes(
            (product / "build-manifest.json").read_bytes()
        )
        db_started = True
        outer_command(docker + [
            "run", "-d", "--name", db_name, "--label", "pgvn.component=linux-admin",
            "--network", "none", "--read-only", "--memory", "512m", "--cpus", "1",
            "--tmpfs", "/var/lib/postgresql:rw,nosuid,noexec,size=384m",
            "--tmpfs", "/var/run/postgresql:rw,nosuid,noexec,size=16m",
            "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=pgvn_admin",
            args.postgres_image, "postgres", "-c", "shared_buffers=32MB",
            "-c", "max_connections=64",
        ])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = db_exec(["pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                             "-d", "pgvn_admin"], expected=None)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("PostgreSQL readiness timed out")
        version = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At", "-U", "postgres",
                           "-d", "pgvn_admin", "-c", "SHOW server_version_num;"]).stdout.strip()
        if version != "180006":
            raise RuntimeError("Expected PostgreSQL 18.6, got " + version)
        db_exec(["psql", "-h", "127.0.0.1", "-X", "-v", "ON_ERROR_STOP=1",
                 "-U", "postgres", "-d", "pgvn_admin"], data=(
                     "CREATE ROLE pgvn_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"
                     "CREATE ROLE pgvn_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"
                     "CREATE ROLE pgvn_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"
                     "CREATE ROLE pgvn_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"
                     "ALTER DATABASE pgvn_admin OWNER TO pgvn_owner;"
                     "CREATE DATABASE pgvn_admin_empty OWNER pgvn_owner;"
                 ))
        owner_dsn = "host=127.0.0.1 port=5432 dbname=pgvn_admin user=pgvn_owner connect_timeout=3"
        admin_dsn = owner_dsn.replace("pgvn_owner", "pgvn_admin")
        service_dsn = owner_dsn.replace("pgvn_owner", "pgvn_service")
        monitor_dsn = owner_dsn.replace("pgvn_owner", "pgvn_monitor")
        empty_dsn = owner_dsn.replace("pgvn_admin", "pgvn_admin_empty")
        builder_test = [
            *docker, "run", "--name", test_name,
            "--network", "container:" + db_name,
            "--user", str(os.getuid()) + ":" + str(os.getgid()),
            "--memory", "1g", "--cpus", "2", "--read-only", "--cap-add=SYS_PTRACE",
            "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=96m",
            "--mount", "type=bind,src=" + str(ROOT) + ",dst=" + str(ROOT),
            "-w", ROOT, "-e", "PGVN_OWNER_DSN", "-e", "PGVN_ADMIN_DSN",
            "-e", "PGVN_SERVICE_DSN", "-e", "PGVN_MONITOR_DSN",
            "-e", "PGVN_EMPTY_DSN", "-e", "ASAN_OPTIONS=detect_leaks=1:abort_on_error=1",
            "-e", "UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1",
            args.builder_image, "python3", "tests/pgvn/run_linux_admin.py",
            "--in-container", "--sdk", sdk, "--configuration", args.configuration,
            "--product", product, "--artifacts", artifacts,
        ]
        if args.sanitize:
            builder_test.insert(-4, "--sanitize")
        test_env = dict(
            environment,
            PGVN_OWNER_DSN=owner_dsn,
            PGVN_ADMIN_DSN=admin_dsn,
            PGVN_SERVICE_DSN=service_dsn,
            PGVN_MONITOR_DSN=monitor_dsn,
            PGVN_EMPTY_DSN=empty_dsn,
        )
        test_started = True
        with (artifacts / "tests.log").open("wb") as test_output:
            process = subprocess.Popen(builder_test, cwd=ROOT, env=test_env,
                                       stdout=test_output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 1200
        lock_started = False
        while process.poll() is None and time.monotonic() < deadline:
            token_request = artifacts / "token-check.request"
            token_result = artifacts / "token-check.result"
            if token_request.exists() and not token_result.exists():
                token_id = token_request.read_text().strip()
                result = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At", "-U",
                                  "postgres", "-d", "pgvn_admin", "-c",
                                  "SELECT encode(token_hash,'hex') FROM pgvn.monitor_tokens "
                                  "WHERE token_id='" + token_id + "';"])
                token_result.write_text(result.stdout.strip(), encoding="ascii")
            lock_request = artifacts / "lock-holder.request"
            lock_held = artifacts / "lock-holder.held"
            if lock_request.exists() and not lock_held.exists():
                outer_command(docker + ["exec", "-d", db_name, "psql", "-h", "127.0.0.1",
                                        "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                                        "-d", "pgvn_admin", "-c",
                                        "BEGIN; LOCK TABLE pgfs.transactions IN ACCESS EXCLUSIVE MODE; "
                                        "SELECT pg_sleep(8); COMMIT;"])
                holder = (
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                    "WHERE a.datname=current_database() AND a.pid<>pg_backend_pid() "
                    "AND l.locktype='relation' AND l.relation='pgfs.transactions'::regclass "
                    "AND l.mode='AccessExclusiveLock' AND l.granted;"
                )
                until = time.monotonic() + 15
                while time.monotonic() < until:
                    current = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At",
                                       "-U", "postgres", "-d", "pgvn_admin", "-c", holder])
                    if current.stdout.strip() == "1":
                        break
                    time.sleep(0.05)
                else:
                    raise TimeoutError("PgFS transaction lock holder was not observed")
                lock_held.write_text(holder + "\n", encoding="utf-8")
                lock_started = True
            lock_wait = artifacts / "lock-wait.request"
            lock_blocked = artifacts / "lock-wait.blocked"
            if lock_wait.exists() and not lock_blocked.exists():
                blocked = (
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                    "WHERE a.datname=current_database() AND a.application_name='pgvn' "
                    "AND a.pid<>pg_backend_pid() AND l.locktype='relation' "
                    "AND l.relation='pgfs.transactions'::regclass AND NOT l.granted "
                    "AND a.wait_event_type='Lock';"
                )
                until = time.monotonic() + 15
                while time.monotonic() < until:
                    current = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At",
                                       "-U", "postgres", "-d", "pgvn_admin", "-c", blocked])
                    if current.stdout.strip() == "1":
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Admin lock-wait command exited before admission")
                    time.sleep(0.05)
                else:
                    raise TimeoutError("Admin command was not observed waiting on PgFS lock")
                lock_blocked.write_text(blocked + "\n", encoding="utf-8")
            lock_released = artifacts / "lock-holder.released"
            lock_wait = artifacts / "lock-wait.request"
            if lock_started and lock_wait.exists() and not lock_released.exists():
                unlocked = (
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                    "WHERE a.datname=current_database() AND a.pid<>pg_backend_pid() "
                    "AND l.locktype='relation' AND l.relation='pgfs.transactions'::regclass "
                    "AND l.mode='AccessExclusiveLock' AND l.granted;"
                )
                current = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At",
                                   "-U", "postgres", "-d", "pgvn_admin", "-c", unlocked])
                if current.stdout.strip() == "0":
                    lock_released.write_text(unlocked + "\n", encoding="utf-8")
            lock_done = artifacts / "lock-wait.done"
            lock_session_clean = artifacts / "lock-wait.session-clean"
            if lock_done.exists() and not lock_session_clean.exists():
                sessions = (
                    "SELECT count(*) FROM pg_stat_activity a "
                    "WHERE a.datname=current_database() AND a.application_name='pgvn' "
                    "AND a.pid<>pg_backend_pid() AND "
                    "(a.wait_event_type='Lock' OR EXISTS (SELECT 1 FROM pg_locks l "
                    "WHERE l.pid=a.pid AND l.locktype='relation' AND "
                    "l.relation='pgfs.transactions'::regclass));"
                )
                current = db_exec(["psql", "-h", "127.0.0.1", "-X", "-At",
                                   "-U", "postgres", "-d", "pgvn_admin", "-c", sessions])
                lock_session_clean.write_text(current.stdout.strip() + "\n", encoding="ascii")
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
            raise TimeoutError("Linux admin suite timed out")
        if process.returncode != 0:
            raise RuntimeError("Linux admin suite failed; see tests.log")
        report = json.loads((artifacts / "admin-suite-report.json").read_text())
        postgres_log = outer_command(docker + ["logs", db_name], timeout=30, expected=None)
        postgres_text = postgres_log.stdout + postgres_log.stderr
        if ("canceling statement due to statement timeout" not in postgres_text or
                "SELECT txn_id FROM pgfs.transactions" not in postgres_text):
            raise RuntimeError("PostgreSQL log did not identify statement_timeout for blocked PgFS SQL")
        report["checks"].append("PostgreSQL log confirms statement_timeout for blocked SQL after SIGINT")
        report["limitations"].append(
            "The SIGINT blocked-SQL case is statement_timeout behavior, not active query cancellation."
        )
        (artifacts / "admin-suite-report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        if report["status"] != "PASS":
            raise RuntimeError("Linux admin suite reported failure")
    except Exception:
        errors.append(traceback.format_exc())
        print(errors[-1], file=sys.stderr, flush=True)
    finally:
        if test_started:
            try:
                outer_command(docker + ["rm", "-f", test_name], timeout=30, expected=None)
            except Exception:
                errors.append(traceback.format_exc())
        if db_started:
            try:
                log = outer_command(docker + ["logs", db_name], timeout=30, expected=None)
                (artifacts / "postgres.log").write_text(log.stdout + log.stderr, encoding="utf-8")
                outer_command(docker + ["rm", "-f", db_name], timeout=30, expected=None)
            except Exception:
                errors.append(traceback.format_exc())
        (artifacts / "orchestration-commands.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in commands), encoding="utf-8"
        )
        report = json.loads((artifacts / "admin-suite-report.json").read_text()) \
            if (artifacts / "admin-suite-report.json").exists() else {}
        result = {
            "format": 1,
            "run_id": run_id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "PASS" if not errors else "FAIL",
            "scope": "Native Linux pgvnadmin and supporting foreground svn:// service",
            "configuration": args.configuration,
            "sanitizers": ["address", "undefined"] if args.sanitize else [],
            "image_ids": image_ids if "image_ids" in locals() else {},
            "builder_os_release": builder_os,
            "host": {"platform": platform.platform(), "os_release": host_os},
            "sdk_manifest_sha256": sha256(args.sdk.resolve() / "sdk-manifest.json"),
            "build_manifest": str(artifacts / "frozen-product/build-manifest.json"),
            "source_snapshot": str(artifacts / "inputs"),
            "checks": report.get("checks", []),
            "limitations": report.get("limitations", []),
            "errors": errors,
            "complete_product": False,
            "artifacts": str(artifacts),
        }
        reproducibility = [
            "# Native Linux admin run", "",
            "- Status: " + result["status"],
            "- Host: " + result["host"]["platform"] + "; " + host_os.strip().replace("\n", "; "),
            "- Builder image: " + args.builder_image + " (" + result["image_ids"].get(args.builder_image, "unavailable") + ")",
            "- Builder OS: " + builder_os.strip().replace("\n", "; "),
            "- PostgreSQL image: " + args.postgres_image + " (" + result["image_ids"].get(args.postgres_image, "unavailable") + ")",
            "- PostgreSQL `server_version_num` was checked as `180006`.",
            "- SDK manifest SHA-256: " + result["sdk_manifest_sha256"],
            "- PostgreSQL ran in a `--network none` container with tmpfs data and no published ports; "
            "the test container joined only its network namespace.",
            "- SQL precreated disposable roles/databases; the real CLI provisioned grants and "
            "initialized/migrated schemas, while SQL supplied only lock and token-hash fixtures.",
            "- SYS_PTRACE was granted only to the disposable test container to inspect its own "
            "child's stdin poll rendezvous; it is not a runtime deployment capability.",
            "- SIGINT does not actively cancel synchronous SQL; the blocked statement exits at "
            "PostgreSQL statement_timeout with SQLSTATE 57014.",
            "- Generated metadata, source inputs, migrations, upstream sources, product files and "
            "`sdk-manifest.json` were frozen under `inputs/` and `frozen-product/`.",
            "- The runner records local image identities; it does not pull images.",
            "",
            "## Docker commands",
        ]
        reproducibility += [
            "```sh\n" + shlex.join(safe_arguments(item["args"])) + "\n```"
            for item in commands if item.get("args") and item["args"][0] == "docker"
        ]
        reproducibility += [
            "",
            "Raw orchestration output is in `orchestration-commands.jsonl`; the admin CLI "
            "command log is `admin-commands.jsonl`; build/test output is in `build.log` and "
            "`tests.log`; PostgreSQL output is in `postgres.log`.",
            "Credential input and generated monitor-token plaintext are redacted and are not "
            "persisted in command logs or errors.",
        ]
        (artifacts / "reproducibility.md").write_text(
            "\n".join(reproducibility) + "\n", encoding="utf-8"
        )
        (artifacts / "report.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"report": str(artifacts / "report.json"),
                          "status": result["status"], "checks": len(result["checks"]),
                          "artifacts": str(artifacts)}), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
