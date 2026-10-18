"""Run the POSIX hook executor against real delegated cgroup v2 containment."""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ARGUMENTS = ["literal ; $(not-shell)", "中文 参数", 'quote " tail\\']
REPOSITORY_ID = "11111111-1111-4111-8111-111111111111"


def script_source(body):
    return "#!/usr/bin/python3\n" + body


def process_state(pid):
    try:
        contents = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    fields = contents[contents.rfind(")") + 1:].split()
    return fields[0] if fields else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--cgroup-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    probe = args.probe.resolve(strict=True)
    cgroup_root = args.cgroup_root.resolve(strict=True)
    if os.geteuid() == 0:
        raise RuntimeError("run the behavioral probe as the delegated service account, not root")
    if not os.access(cgroup_root, os.W_OK) or not (cgroup_root / "cgroup.kill").exists():
        raise RuntimeError("cgroup root must be writable cgroup v2 with cgroup.kill")
    manifest_path = probe.parents[1] / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = args.output or ROOT / "artifacts/pgvn/tests" / (
        "linux-hooks-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = []
    failures = []
    checks = []
    environment = dict(os.environ)
    environment.update({"PGVN_DSN": "must-not-be-inherited",
                        "PGPASSWORD": "must-not-be-inherited",
                        "PGVN_UNLISTED": "must-not-be-inherited",
                        "PGSERVICEFILE": "/tmp/pgservice.conf",
                        "PGSYSCONFDIR": "/etc/postgresql"})

    def run_case(name, body, mode="normal", timeout=5, resolve="direct",
                 expected_pass=True, expected_message=None, executable=True,
                 unavailable_cgroup=False, file_kind="normal", assert_not_run=False,
                 root_symlink=False, maximum_output=None, missing_cgroup_root=False,
                 environment_overrides=None, relative_cgroup_root=False,
                 symlink_cgroup_root=False):
        nonlocal records
        with tempfile.TemporaryDirectory(prefix="pgvn-hook-case-") as temporary:
            work = Path(temporary)
            hook_root = work / "hooks"
            hook_root.mkdir()
            marker = work / "inherited-fd-marker"
            marker.write_text("not inherited", encoding="utf-8")
            touched = work / "executed"
            pidfile = work / "descendant.pid"
            source = body.replace("@TOUCHED@", json.dumps(str(touched))).replace(
                "@PIDFILE@", json.dumps(str(pidfile))).replace(
                "@MARKER@", json.dumps(str(marker)))
            hook = hook_root / "probe-hook"
            if file_kind == "missing":
                pass
            elif file_kind == "directory":
                hook.mkdir()
            elif file_kind == "symlink":
                target = hook_root / "probe-target"
                target.write_text(script_source(source), encoding="utf-8")
                target.chmod(0o755)
                hook.symlink_to(target.name)
            else:
                hook.write_text(script_source(source), encoding="utf-8")
                hook.chmod(0o755 if executable else 0o644)
            dispatch_root = hook_root
            if root_symlink:
                dispatch_root = work / "hook-root-link"
                dispatch_root.symlink_to(hook_root.name, target_is_directory=True)
            cgroup = work / "not-a-cgroup" if unavailable_cgroup else cgroup_root
            if unavailable_cgroup:
                cgroup.mkdir()
            elif symlink_cgroup_root:
                cgroup = work / "cgroup-link"
                cgroup.symlink_to(cgroup_root, target_is_directory=True)
            started = time.monotonic()
            cgroup_argument = "" if missing_cgroup_root else "relative" if relative_cgroup_root else str(cgroup)
            command = [str(probe), mode, str(dispatch_root), cgroup_argument,
                       "probe-hook", str(timeout), str(marker), resolve]
            case_environment = environment.copy()
            if environment_overrides:
                case_environment.update(environment_overrides)
            result = subprocess.run(command,
                                    env=case_environment, capture_output=True, text=True, timeout=15)
            record = {"name": name, "command": command, "returncode": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr,
                      "elapsed_seconds": time.monotonic()-started,
                      "script_sha256": hashlib.sha256(hook.read_bytes()).hexdigest()
                      if hook.is_file() else None}
            records.append(record)
            passed = (result.returncode == 0) == expected_pass
            if expected_message:
                passed = passed and expected_message in result.stdout + result.stderr
            if maximum_output is not None:
                passed = passed and len(result.stdout)+len(result.stderr)<=maximum_output
            if assert_not_run:
                passed = passed and not touched.exists()
            if name in ("setsid-timeout", "setsid-cancellation", "success-kills-descendant"):
                if not pidfile.exists():
                    passed = False
                    record["descendant_pid_missing"] = True
                else:
                    pid = int(pidfile.read_text(encoding="ascii"))
                    deadline = time.monotonic()+3
                    state = process_state(pid)
                    while state not in (None, "Z", "X") and time.monotonic()<deadline:
                        time.sleep(.05)
                        state = process_state(pid)
                    passed = passed and state in (None, "Z", "X")
                    record["descendant_pid"] = pid
                    record["descendant_state"] = state
            child_directories = [entry.name for entry in cgroup_root.iterdir() if entry.is_dir()]
            passed = passed and not child_directories
            record["remaining_cgroup_directories"] = child_directories
            if not passed:
                failures.append(name)
            checks.append({"name": name, "passed": passed})
            if unavailable_cgroup:
                shutil.rmtree(cgroup)

    valid = r'''
import base64, json, os, sys
event = json.loads(sys.stdin.buffer.read())
assert event["event"] == "pre-commit"
assert event["repository_id"] == "@REPOSITORY_ID@"
assert event["username"] == "测试用户"
assert event["path"] == "/trunk/文件"
assert event["property_value_base64"] == "@PROPERTY_BASE64@"
assert event["paths"] == ["/trunk/文件", "/trunk/第二个"]
assert sys.argv[1:] == @ARGUMENTS@
assert os.environ["PGVN_EVENT"] == "pre-commit"
assert os.environ["PGVN_REPOSITORY_ID"] == "@REPOSITORY_ID@"
assert os.environ["PGVN_USER"] == "测试用户"
assert os.environ["PGVN_TRANSACTION"] == "txn-42"
assert os.environ["PGVN_REVISION"] == "42"
assert os.environ["PGVN_CONFIG"] == "/etc/pgvn/hooks.ini"
assert os.environ["PGSERVICEFILE"] == "/tmp/pgservice.conf"
assert os.environ["PGSYSCONFDIR"] == "/etc/postgresql"
for name in ("PGPASSWORD", "PGVN_DSN", "PGVN_UNLISTED", "LD_PRELOAD"):
    assert name not in os.environ, name
for name in os.listdir("/proc/self/fd"):
    try:
        assert @MARKER@ not in os.readlink("/proc/self/fd/" + name)
    except FileNotFoundError:
        pass
open(@TOUCHED@, "w").close()
'''
    valid = (valid.replace("@REPOSITORY_ID@", REPOSITORY_ID)
             .replace("@PROPERTY_BASE64@", base64.b64encode(b"a\0b\xff").decode("ascii"))
             .replace("@ARGUMENTS@", json.dumps(EXPECTED_ARGUMENTS, ensure_ascii=False)))
    run_case("json-utf8-arguments-environment-and-fd", valid, resolve="resolve")
    run_case("rejected-hook-output", "import sys\nsys.stdin.buffer.read()\nsys.stderr.write('提交被策略拒绝')\nsys.exit(3)\n",
             expected_pass=False, expected_message="提交被策略拒绝")
    run_case("output-flood-is-drained", "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'x'*200000)\nsys.stderr.buffer.write(b'y'*200000)\n")
    run_case("rejection-output-is-bounded",
             "import sys\nsys.stdin.buffer.read()\nsys.stderr.buffer.write(b'x'*200000)\nsys.exit(3)\n",
             expected_pass=False, expected_message="with output:", maximum_output=11000)
    run_case("valid-lock-token", "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('  token-中文  \\n')\n",
             mode="token", expected_message="PASS token=token-中文")
    run_case("invalid-lock-token", "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('first\\nsecond\\n')\n",
             mode="bad-token", expected_pass=False, expected_message="invalid token")
    run_case("missing-executable-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="missing, non-executable", resolve="direct",
             file_kind="missing", assert_not_run=True)
    run_case("non-regular-executable-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="not a regular file", file_kind="directory",
             assert_not_run=True)
    run_case("non-executable-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="missing, non-executable", executable=False,
             assert_not_run=True)
    run_case("symlink-executable-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="missing, non-executable", file_kind="symlink",
             assert_not_run=True)
    run_case("symlink-hook-root-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="could not securely open hook-root",
             root_symlink=True, assert_not_run=True)
    run_case("unavailable-cgroup-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="requires a writable cgroup v2",
             unavailable_cgroup=True, assert_not_run=True)
    run_case("missing-cgroup-root-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="not fully configured",
             missing_cgroup_root=True, assert_not_run=True)
    run_case("relative-cgroup-root-rejected", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="hook-cgroup-root must be an absolute path",
             relative_cgroup_root=True, assert_not_run=True)
    run_case("symlink-cgroup-root-rejected", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="requires a writable cgroup v2",
             symlink_cgroup_root=True, assert_not_run=True)
    run_case("oversized-argument-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             mode="large-argument", expected_pass=False,
             expected_message="arguments exceed the UTF-8 resource limits", assert_not_run=True)
    run_case("oversized-environment-fails-closed", "open(@TOUCHED@, 'w').close()\n",
             expected_pass=False, expected_message="arguments or environment exceed",
             environment_overrides={"TMPDIR": "x" * 4097}, assert_not_run=True)
    run_case("oversized-binary-property-fails-closed",
             "open(@TOUCHED@, 'w').close()\n", mode="large-property",
             expected_pass=False, expected_message="event exceeds its resource limit",
             assert_not_run=True)
    run_case("setsid-timeout", "import os, time\npid=os.fork()\nif pid == 0:\n os.setsid()\n child=os.fork()\n if child: os._exit(0)\n open(@PIDFILE@,'w').write(str(os.getpid()))\n time.sleep(60)\n os._exit(0)\nos.waitpid(pid,0)\nwhile not os.path.exists(@PIDFILE@): time.sleep(.01)\ntime.sleep(60)\n",
             timeout=1, expected_pass=False, expected_message="exceeded its timeout")
    run_case("setsid-cancellation", "import os, time\npid=os.fork()\nif pid == 0:\n os.setsid()\n child=os.fork()\n if child: os._exit(0)\n open(@PIDFILE@,'w').write(str(os.getpid()))\n time.sleep(60)\n os._exit(0)\nos.waitpid(pid,0)\nwhile not os.path.exists(@PIDFILE@): time.sleep(.01)\ntime.sleep(60)\n",
             mode="cancel", timeout=10, expected_pass=False, expected_message="cancelled")
    run_case("success-kills-descendant", "import os, sys, time\npid=os.fork()\nif pid == 0:\n os.setsid()\n child=os.fork()\n if child: os._exit(0)\n open(@PIDFILE@,'w').write(str(os.getpid()))\n time.sleep(60)\n os._exit(0)\nos.waitpid(pid,0)\nwhile not os.path.exists(@PIDFILE@): time.sleep(.01)\nsys.exit(0)\n")

    try:
        os_release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        os_release = {}
    report = {"format": 1, "status": "FAIL" if failures else "PASS",
              "scope": "POSIX hook executor component; not Linux product acceptance",
              "host": {"os": platform.platform(), "os_release": os_release,
                       "kernel": platform.release(), "python": platform.python_version(),
                       "architecture": platform.machine(), "uid": os.geteuid()},
              "build_identity": manifest["build_identity"],
              "sdk_manifest_sha256": manifest["sdk_manifest_sha256"],
              "compiler": manifest["compiler"], "sanitizers": manifest["sanitizers"], "probe": str(probe),
              "cgroup_root": str(cgroup_root), "checks": checks, "runs": records,
              "failures": failures}
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "passed": sum(check["passed"] for check in checks),
                      "total": len(checks), "report": str(report_path)}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
