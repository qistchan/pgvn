"""Run independent pinned regression suites against one fixed PgFS build."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FILE_SUITES = ("basic", "copy", "commit", "log", "merge", "update", "blame", "diff",
               "switch", "prop", "lock", "svnsync", "mergeinfo", "merge_automatic",
               "merge_reintegrate", "svnlook", "history", "import", "export", "depth",
               "externals", "special", "iprop", "info", "checkout", "cat", "pegrev_parse",
               "relocate", "svnversion", "svndumpfilter", "svnmucc")
SERVICE_SUITES = (("authz", "svn"), ("authz", "http"), ("mod_dav_svn", "http"),
                  ("mod_authz_svn", "http"), ("svnrdump", "svn"), ("svnrdump", "http"),
                  ("svnsync", "svn"), ("svnsync", "http"), ("lock", "svn"), ("lock", "http"),
                  ("merge_authz", "svn"), ("merge_authz", "http"),
                  ("svnsync_authz", "svn"), ("svnsync_authz", "http"),
                  ("iprop_authz", "svn"), ("iprop_authz", "http"),
                  ("basic", "http"), ("copy", "http"), ("commit", "http"))


def job(suite, transport):
    name = suite + "-" + transport
    command = [sys.executable, str(ROOT / "tests/run_upstream.py"), suite,
               "--transport", transport]
    print("Start", name, flush=True)
    result = subprocess.run(command, cwd=ROOT, capture_output=True)
    print("Finished", name, "exit", result.returncode, flush=True)
    print(result.stdout.decode("utf8", "replace").encode("ascii", "backslashreplace").decode(), flush=True)
    if result.returncode:
        print(result.stderr.decode("utf8", "replace").encode("ascii", "backslashreplace").decode(), flush=True)
    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("file", "services"):
        raise SystemExit("run_compatibility.py file|services")
    jobs = [(suite, "file") for suite in FILE_SUITES] if sys.argv[1] == "file" else SERVICE_SUITES
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = [future.result() for future in as_completed([executor.submit(job, *args) for args in jobs])]
    raise SystemExit(any(results))
