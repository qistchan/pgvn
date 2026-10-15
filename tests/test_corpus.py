"""Authorized read-only Polarion export and isolated PgFS history validation."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import configparser
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from verify_corpus_properties import dump_revision_properties, verify_revision_properties

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"C:\Polarion\data\svn\repo")
ORIGINAL = Path(r"C:\Polarion\bundled\svn\bin")
BIN = ROOT / "runtime/svn/bin"
DSN = "host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable"
ENV = dict(os.environ, PGFS_DSN=DSN)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(program, *args):
    result = subprocess.run([str(program), *map(str, args)], env=ENV, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf8", "replace"))
    return result.stdout


def file_run(program, args, stdout_path, stdin_path=None):
    started = time.monotonic()
    with stdout_path.open("wb") as output, stdout_path.with_suffix(".stderr.log").open("wb") as error:
        if stdin_path:
            with stdin_path.open("rb") as input_file:
                process = subprocess.Popen([str(program), *map(str, args)], env=ENV,
                                           stdin=input_file, stdout=output, stderr=error)
                print("Process", process.pid, "log", stdout_path, flush=True)
                code = process.wait()
        else:
            process = subprocess.Popen([str(program), *map(str, args)], env=ENV, stdout=output, stderr=error)
            print("Process", process.pid, "log", stdout_path, flush=True)
            code = process.wait()
    if code:
        raise RuntimeError("Corpus stage failed: " + stdout_path.with_suffix(".stderr.log").read_text(encoding="utf8", errors="replace")[-3000:])
    return round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, help="Reuse an authorized fixed export and completed PgFS import under runtime/corpus")
    args = parser.parse_args()
    digest = sha(BIN / "libsvn_fs_pg-1.dll")
    source_head = int(read(ORIGINAL / "svnlook.exe", "youngest", SOURCE))
    source_uuid = read(ORIGINAL / "svnlook.exe", "uuid", SOURCE).decode("ascii").strip()
    stages = {}
    if args.resume:
        fixture = args.resume.resolve()
        assert fixture.is_relative_to((ROOT / "runtime/corpus").resolve())
        archive, target = fixture / "source.dump", fixture / "pg"
        revisions = dump_revision_properties(archive)
        head = max(revisions)
        assert set(revisions) == set(range(head + 1)) and source_head >= head
        with archive.open("rb") as stream:
            archive_uuid = re.search(rb"(?m)^UUID: ([a-f0-9-]+)$", stream.read(1024)).group(1).decode("ascii")
        assert archive_uuid == source_uuid
        config = configparser.ConfigParser()
        config.read(target / "db/pgfs.conf")
        assert config["pgfs"]["connection"] == DSN
        assert (target / "db/fs-type").read_text().strip() == "pg"
        print("Reusing authorized fixed export and completed PgFS import through r" + str(head), flush=True)
    else:
        head = source_head
        fixture = ROOT / "runtime/corpus" / ("polarion-r" + str(head) + "-" + uuid.uuid4().hex)
        fixture.mkdir(parents=True)
        archive, target = fixture / "source.dump", fixture / "pg"
        print("Exporting fixed Polarion revisions 0:" + str(head), "to", fixture, flush=True)
        stages["export_seconds"] = file_run(ORIGINAL / "svnadmin.exe", ["dump", "--deltas", "-r", "0:" + str(head), SOURCE], archive)
        read(BIN / "svnadmin.exe", "create", "--fs-type", "pg", target)
        print("Loading archived revisions into isolated PgFS repository", flush=True)
        stages["load_seconds"] = file_run(BIN / "svnadmin.exe", ["load", "--force-uuid", target], fixture / "load.log", archive)
        (fixture / "import-provenance.json").write_text(json.dumps({"dll_sha256": digest, "revisions_loaded": head + 1,
            "archive_sha256": sha(archive)}, indent=2) + "\n", encoding="utf8")
    assert int(read(BIN / "svnlook.exe", "youngest", target)) == head
    assert read(BIN / "svnlook.exe", "uuid", target).decode("ascii").strip() == source_uuid
    print("Verifying every published PgFS revision", flush=True)
    stages["verify_seconds"] = file_run(BIN / "svnadmin.exe", ["verify", target], fixture / "verify.log")
    snapshots = []
    for rev in sorted({0, 1, head // 2, max(0, head - 1), head}):
        print("Comparing full logical snapshot for revision", rev, flush=True)
        old, new = fixture / ("source-r" + str(rev) + ".snapshot"), fixture / ("pg-r" + str(rev) + ".snapshot")
        file_run(BIN / "fs_probe.exe", ["snapshot", SOURCE, rev], old)
        file_run(BIN / "fs_probe.exe", ["snapshot", target, rev], new)
        source_data, target_data = old.read_bytes(), new.read_bytes()
        if source_data != target_data:
            import difflib
            difference = "".join(difflib.unified_diff(source_data.decode("ascii").splitlines(True),
                                                     target_data.decode("ascii").splitlines(True), n=1))
            (fixture / ("r" + str(rev) + ".diff")).write_text(difference, encoding="utf8")
            raise AssertionError("Corpus snapshot differs at revision " + str(rev) + "; see " + str(fixture))
        snapshots.append({"revision": rev, "sha256": sha(old), "nodes": sum(line.startswith(b"N|") for line in source_data.splitlines())})
    print("Comparing all changed-path records", flush=True)
    for rev in range(head + 1):
        old = read(ORIGINAL / "svnlook.exe", "changed", "--copy-info", "-r", rev, SOURCE)
        new = read(BIN / "svnlook.exe", "changed", "--copy-info", "-r", rev, target)
        assert old == new, "Changed paths differ at revision " + str(rev)
    print("Comparing all revision properties against the fixed export", flush=True)
    revision_properties = verify_revision_properties(fixture, target, head)
    assert sha(BIN / "libsvn_fs_pg-1.dll") == digest, "Module changed during corpus validation"
    report = {"revision_properties": revision_properties, "timestamp": datetime.now(timezone.utc).isoformat(), "source": str(SOURCE),
              "source_head_at_start": head, "source_head_at_end": int(read(ORIGINAL / "svnlook.exe", "youngest", SOURCE)),
              "source_uuid": source_uuid, "fixture": str(fixture), "target": str(target),
              "archive_sha256": sha(archive), "archive_bytes": archive.stat().st_size,
              "all_revisions_verified": head + 1, "changed_path_revisions_compared": head + 1,
              "snapshots": snapshots, "passed": True, "resumed_import": bool(args.resume),
              "stage_seconds": stages, "dll_sha256": digest}
    provenance = fixture / "import-provenance.json"
    if provenance.is_file():
        report["import_provenance"] = json.loads(provenance.read_text(encoding="utf8"))
    (ROOT / "artifacts/tests/polarion-corpus-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({"passed": True, "revisions": head + 1, "snapshots": len(snapshots), "fixture": str(fixture)}, indent=2))


if __name__ == "__main__":
    main()
