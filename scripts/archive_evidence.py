"""Preserve a completed test/build snapshot before replacing its runtime."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
manifest = ROOT / "artifacts/build/manifest.json"
data = json.loads(manifest.read_text())
digest = hashlib.sha256((ROOT / "runtime/svn/bin/libsvn_fs_pg-1.dll").read_bytes()).hexdigest()
assert data["dll_sha256"] == digest
parent = (ROOT / "artifacts/history").resolve()
destination = (parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + digest[:12])).resolve()
assert destination.is_relative_to(parent)
destination.mkdir(parents=True)
shutil.copy2(manifest, destination / "build-manifest.json")
for source in (ROOT / "artifacts/tests").iterdir():
    if source.is_file() and source.suffix in (".json", ".log"):
        shutil.copy2(source, destination / source.name)
print(destination)
