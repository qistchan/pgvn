"""Reproduce the fixed-build native SVN gate, keeping DB restarts sequential."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONIOENCODING="utf8")


def run(name, args):
    log = ROOT / "artifacts/tests" / ("gate-" + name + ".log")
    print("Start", name, flush=True)
    with log.open("wb") as output:
        result = subprocess.run([sys.executable, *args], cwd=ROOT, env=ENV,
                                stdout=output, stderr=subprocess.STDOUT)
    print("Finished", name, "exit", result.returncode, "log", log, flush=True)
    return result.returncode


if __name__ == "__main__":
    # These two deliberately restart only the validated dedicated cluster.
    for name in ("p0", "recovery"):
        if run(name, ["tests/test_" + name + ".py"]):
            raise SystemExit(1)
    jobs = [("file-matrix", ["tests/run_compatibility.py", "file"]),
            ("service-matrix", ["tests/run_compatibility.py", "services"]),
            ("native-fs", ["tests/run_native_tests.py", "fs", "--applicable"]),
            ("native-repos", ["tests/run_native_tests.py", "repos", "--applicable"]),
            ("svnadmin", ["tests/run_upstream.py", "svnadmin", "--applicable"])]
    jobs.extend((name, ["tests/test_" + name + ".py"])
                for name in ("p1", "p2", "admin", "faults", "services", "synthetic_corpus"))
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = [future.result() for future in as_completed([executor.submit(run, name, args) for name, args in jobs])]
    if any(results):
        raise SystemExit(1)
    raise SystemExit(subprocess.run([sys.executable, "scripts/verify_evidence.py"], cwd=ROOT, env=ENV).returncode)
