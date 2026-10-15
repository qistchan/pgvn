"""Execute unchanged Apache native semantic tests in an isolated workspace."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if len(sys.argv) < 2 or sys.argv[1] not in ("fs", "repos"):
    raise SystemExit("run_native_tests.py fs|repos [--fs-type pg|fsfs] [test numbers]")
name = sys.argv[1]
options = sys.argv[2:]
applicable = "--applicable" in options
if applicable:
    options.remove("--applicable")
fs_type = "pg"
if "--fs-type" in options:
    i = options.index("--fs-type")
    fs_type = options[i + 1]
    options = options[:i] + options[i + 2:]
if fs_type not in ("pg", "fsfs"):
    raise SystemExit("Unsupported backend")
parent = (ROOT / "runtime/upstream").resolve()
directory = (parent / (name + "-native-" + fs_type + "-" + uuid.uuid4().hex)).resolve()
assert directory.is_relative_to(parent)
directory.mkdir(parents=True)
program = ROOT / "runtime/svn/bin" / (name + "-test.exe")
log = ROOT / "artifacts/tests" / ("native-" + name + "-" + fs_type + ".log")
env = dict(os.environ, PGFS_DSN="host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable")
print("Running native", name, "against", fs_type, "in", directory, flush=True)
requested = list(options)
exclusions = {}
if applicable and fs_type == "pg":
    exclusions = ({40: "Hardcodes format 3 and SVN 1.5 support for FSFS/BDB; PgFS format 1 is checked separately",
                   65: "Requires the FSFS/FSX SQLite rep-cache file and its locking mechanism",
                   67: "Overwrites the FSFS/FSX rep-cache and representation files to construct a SHA1 collision"}
                  if name == "fs" else
                  {21: "Assumes every non-FSX backend existed before SVN 1.4 and can create pre-1.4 repository formats"})
    if options:
        raise SystemExit("--applicable cannot be combined with a test selection")
    options = [str(i) for i in range(1, 71 if name == "fs" else 33) if i not in exclusions]
srcdir = ROOT / "vendor/subversion-1.14.5/subversion/tests" / ("libsvn_" + name)
with log.open("wb") as output:
    process = subprocess.run([str(program), "--fs-type=" + fs_type, "--srcdir=" + str(srcdir), *options], cwd=directory,
                             env=env, stdout=output, stderr=subprocess.STDOUT)
text = log.read_text(encoding="utf8", errors="replace")
report = {"timestamp": datetime.now(timezone.utc).isoformat(), "suite": name, "fs_type": fs_type,
          "exit_code": process.returncode, "selection": options, "workspace": str(directory),
          "counts": dict(Counter(re.findall(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):", text, re.M))),
          "dll_sha256": hashlib.sha256((ROOT / "runtime/svn/bin/libsvn_fs_pg-1.dll").read_bytes()).hexdigest(),
          "test_binary_sha256": hashlib.sha256(program.read_bytes()).hexdigest()}
report["requested_selection"] = requested
report["applicable_matrix"] = applicable
report["backend_specific_exclusions"] = exclusions
log.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
print(text[-16000:], flush=True)
raise SystemExit(process.returncode)
