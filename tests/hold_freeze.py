"""Bounded child for the native svnadmin freeze integration test."""
from pathlib import Path
import sys
import time
marker, release = map(Path, sys.argv[1:3])
marker.write_text("frozen", encoding="ascii")
deadline = time.monotonic() + 20
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
raise SystemExit(0 if release.exists() else 1)
