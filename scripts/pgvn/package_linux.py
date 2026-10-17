"""Create a verifiable Linux development snapshot; final release gates stay separate."""
from pathlib import Path
import argparse
import json
import os
import shutil
import tarfile
from build_metadata import collect, encoded, sha256
from check_pair import load_package

ROOT = Path(__file__).resolve().parents[2]


def package(product, output, allow_dirty=False):
    product = product.resolve(strict=True)
    build = json.loads((product / "build-manifest.json").read_text())
    current = collect(ROOT, "linux", build["build_identity"]["configuration"])
    if any(build[k] != current[k] for k in current):
        raise ValueError("Build inputs changed; rebuild before packaging")
    identity = build["build_identity"]
    if identity["git_worktree_dirty"] and not allow_dirty:
        raise ValueError("Commit build inputs first, or explicitly allow a dirty development snapshot")
    if not build.get("complete_product"):
        raise ValueError("A complete Linux product build is required")
    stem = "PgVN-" + identity["version"] + "-linux-x64-" + sha256(product / "build-manifest.json")[:16]
    destination = output / stem
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o755)
    files = {}

    def copy(relative, source, expected=None):
        if source.is_symlink() and not source.resolve().is_relative_to(product):
            raise ValueError("Runtime link escapes product")
        if expected and sha256(source) != expected:
            raise ValueError("Changed package input: " + relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        executable = relative.startswith(("bin/", "apache/bin/")) or relative.endswith("pgvnctl.py")
        target.chmod(0o755 if executable else 0o644)
        files[relative] = {"sha256": sha256(target), "bytes": target.stat().st_size}

    for relative, entry in build["products"].items():
        name = relative.removeprefix("runtime/pgvn/")
        copy(name, product / name, entry["sha256"])
    copy("build-manifest.json", product / "build-manifest.json")
    for path in sorted((ROOT / "deploy/linux").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(ROOT).as_posix()
            copy(relative, path, build["sources"][relative])
    copy("source-reference/version.json", ROOT / "src/pgvn/version.json", build["sources"]["src/pgvn/version.json"])
    for namespace, migrations in build["migrations"].items():
        for name, expected in migrations.items():
            relative = ("pgvn/" if namespace == "pgvn" else "") + name
            copy("schema-reference/" + relative, ROOT / "sql" / relative, expected)
    for directory in destination.rglob("*"):
        if directory.is_dir(): directory.chmod(0o755)
    info = {"format": 1, "package_kind": "development-snapshot", "version": identity["version"],
            "git_base_commit": identity["git_commit"], "build_identity": identity,
            "build_manifest_sha256": sha256(product / "build-manifest.json"), "files": files,
            "acceptance": {"release_accepted": False, "windows": "deferred-by-user",
                           "systemd_reboot": "separate-package-bound-evidence", "evidence": "recorded separately"}}
    manifest = destination / "package-manifest.json"
    manifest.write_bytes(json.dumps(info, indent=2, sort_keys=True).encode() + b"\n")
    (destination / "package-manifest.json.sha256").write_text(sha256(manifest) + "  package-manifest.json\n")
    load_package(destination)
    archive = output / (stem + ".tar.gz")
    # gzip timestamp is fixed as well as tar metadata.
    import gzip
    with archive.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for path in sorted(destination.rglob("*")):
                member = tar.gettarinfo(str(path), arcname=stem + "/" + path.relative_to(destination).as_posix())
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                if path.is_file():
                    with path.open("rb") as stream:
                        tar.addfile(member, stream)
                else:
                    tar.addfile(member)
    (output / (archive.name + ".sha256")).write_text(sha256(archive) + "  " + archive.name + "\n")
    return {"directory": str(destination), "archive": str(archive), "sha256": sha256(archive)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/pgvn/linux")
    parser.add_argument("--allow-dirty", action="store_true")
    options = parser.parse_args()
    print(json.dumps(package(options.product, options.output, options.allow_dirty)))
