"""Verify extracted Windows/Linux package provenance; does not grant acceptance."""
from pathlib import Path, PurePosixPath
import argparse
import json

from build_metadata import VERSION_PATH, check_pair, check_runtime, encoded, object_sha256, sha256, validate_version


def load_package(directory):
    directory = directory.resolve()
    path = directory / "package-manifest.json"
    checksum = (directory / "package-manifest.json.sha256").read_text(encoding="ascii").split()[0]
    if sha256(path) != checksum:
        raise ValueError("Package manifest checksum mismatch")
    package = json.loads(path.read_text(encoding="utf-8"))
    for relative, entry in package["files"].items():
        parts = PurePosixPath(relative)
        if not relative or parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
            raise ValueError("Invalid package path: " + relative)
        target = directory / relative
        if not target.resolve().is_relative_to(directory) or target.is_symlink():
            raise ValueError("Package path escapes its root: " + relative)
        if sha256(target) != entry["sha256"] or target.stat().st_size != entry["bytes"]:
            raise ValueError("Package file mismatch: " + relative)
    build_path = directory / "build-manifest.json"
    if "build-manifest.json" not in package["files"] or sha256(build_path) != package["build_manifest_sha256"]:
        raise ValueError("Package/build manifest mismatch")
    build = json.loads(build_path.read_text(encoding="utf-8"))
    identity = package["build_identity"]
    if encoded(identity) != encoded(build["build_identity"]):
        raise ValueError("Package/build identity mismatch")
    if object_sha256(build["sources"]) != identity["source_manifest_sha256"]:
        raise ValueError("Source inventory mismatch")
    if object_sha256(build["migrations"]) != identity["migrations_sha256"]:
        raise ValueError("Migration inventory mismatch")
    descriptor_path = directory / "source-reference/version.json"
    if "source-reference/version.json" not in package["files"] or sha256(descriptor_path) != build["sources"][VERSION_PATH]:
        raise ValueError("Packaged version descriptor mismatch")
    descriptor = validate_version(json.loads(descriptor_path.read_text(encoding="utf-8")))
    for name in ("product", "version", "milestone", "upstream", "schemas", "api_version", "cache_format_version", "capabilities"):
        if encoded(descriptor[name]) != encoded(identity[name]):
            raise ValueError("Version descriptor/build identity mismatch: " + name)
    if identity["svn_base"] != descriptor["upstream"]["subversion"]:
        raise ValueError("Subversion base mismatch")
    if set(build["migrations"]) != {"pgvn", "pgfs"}:
        raise ValueError("Missing migration namespace")
    if len(build["migrations"]["pgvn"]) != descriptor["schemas"]["catalog"] or len(build["migrations"]["pgfs"]) != descriptor["schemas"]["fs"]:
        raise ValueError("Migration inventory/schema mismatch")
    for namespace, migrations in build["migrations"].items():
        if namespace not in ("pgvn", "pgfs"):
            raise ValueError("Unknown migration namespace")
        for name, expected in migrations.items():
            relative = "schema-reference/" + ("pgvn/" if namespace == "pgvn" else "") + name
            if "/" in name or "\\" in name or ":" in name or relative not in package["files"] or package["files"][relative]["sha256"] != expected:
                raise ValueError("Packaged migration mismatch")
    if package["version"] != identity["version"] or package["git_base_commit"] != identity["git_commit"]:
        raise ValueError("Package version/commit mismatch")
    required = {"bin/pgvnserve", "bin/pgvnadmin", "apache/modules/mod_dav_pgvn.so", "apache/modules/mod_authz_pgvn.so"}
    if identity["target_os"] == "windows":
        required = {n + ".exe" if n.startswith("bin/") else n for n in required}
    products = set()
    for relative, entry in build["products"].items():
        if not relative.startswith("runtime/pgvn/"):
            raise ValueError("Invalid product path: " + relative)
        packaged = relative.removeprefix("runtime/pgvn/")
        if package["files"][packaged]["sha256"] != entry["sha256"]:
            raise ValueError("Package product mismatch: " + packaged)
        products.add(packaged)
    if not required.issubset(products):
        raise ValueError("Missing native package products")
    for name in ("pgvnserve", "pgvnadmin"):
        check_runtime(build["runtime_build_info"][name], identity)
    if identity["target_os"] == "windows":
        if encoded(build["apache_host"]["runtime"]["build_identity"]) != encoded(identity):
            raise ValueError("Apache host/build identity mismatch")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, required=True, help="extracted Windows package root")
    parser.add_argument("--linux", type=Path, required=True, help="extracted Linux package root")
    args = parser.parse_args()
    try:
        windows, linux = load_package(args.windows), load_package(args.linux)
        check_pair(windows, linux)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        parser.exit(1, "Package pairing rejected: " + str(error) + "\n")
    print(json.dumps({"provenance_match": True, "version": windows["version"],
                      "git_commit": windows["git_commit"], "functional_acceptance": "not_checked"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
