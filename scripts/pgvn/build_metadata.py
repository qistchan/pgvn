"""Shared, SDK-independent version and source identity for PgVN builds."""
from pathlib import Path
import hashlib
import json
import re
import subprocess

from embed_migrations import CATALOG, STORAGE

VERSION_PATH = "src/pgvn/version.json"
SOURCE_GROUPS = (
    ("deploy/linux", {".py", ".service", ".example", ".md"}),
    ("src", None), ("web", None), ("sql", None), ("patches/pgvn", None),
    ("scripts/pgvn", {".py", ".ps1", ".json", ".Dockerfile"}),
    ("tests/pgvn", {".py", ".ps1", ".c", ".h", ".inc", ".java", ".Dockerfile", ".service"}),
)
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_sha256(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def source_inventory(root):
    sources = {"scripts/build.py": sha256(root / "scripts/build.py")}
    for directory, suffixes in SOURCE_GROUPS:
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and (suffixes is None or path.suffix in suffixes):
                sources[path.relative_to(root).as_posix()] = sha256(path)
    if VERSION_PATH not in sources:
        raise ValueError("Missing shared product version: " + VERSION_PATH)
    return sources


def read_version(root):
    version = json.loads((root / VERSION_PATH).read_text(encoding="utf-8"))
    return validate_version(version)


def validate_version(version):
    if version.get("format") != 1 or version.get("product") != "PgVN":
        raise ValueError("Unsupported PgVN version descriptor")
    if not re.fullmatch(SEMVER, version["version"]):
        raise ValueError("Invalid product version")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", version["milestone"]):
        raise ValueError("Invalid historical milestone")
    for name in ("catalog", "fs"):
        if type(version["schemas"][name]) is not int or version["schemas"][name] < 1:
            raise ValueError("Invalid schema version: " + name)
    for name, minimum in (("api_version", 1), ("cache_format_version", 0)):
        if type(version[name]) is not int or version[name] < minimum:
            raise ValueError("Invalid version: " + name)
    for name in ("subversion", "apr", "apr_util", "httpd", "postgresql"):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version["upstream"][name]):
            raise ValueError("Invalid upstream version: " + name)
    if not re.fullmatch(r"[1-9][0-9]+\.(0|[1-9][0-9]*)", version["upstream"]["postgresql"]):
        raise ValueError("Expected a PostgreSQL 10+ major.minor client version")
    required = {"repository_by_id", "svn_protocol", "dav_protocol", "svn_authz_bypass",
                "cache_local", "cache_redis", "monitor"}
    if set(version["capabilities"]) != required:
        raise ValueError("Unexpected capability descriptor")
    for name, value in version["capabilities"].items():
        if (name == "monitor" and value not in ("full", "none")) or (name != "monitor" and type(value) is not bool):
            raise ValueError("Invalid capability: " + name)
    return version


def migration_inventory(root, version):
    result = {}
    for namespace, directory, names, schema in (
        ("pgvn", "sql/pgvn", CATALOG, "catalog"), ("pgfs", "sql", STORAGE, "fs")
    ):
        actual = {p.name for p in (root / directory).glob("*.sql")}
        if actual != set(names) or len(names) != version["schemas"][schema]:
            raise ValueError("Migration list/schema mismatch: " + namespace)
        result[namespace] = {name: sha256(root / directory / name) for name in names}
    return result


def git_state(root):
    def git(*args):
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), *args],
                                       encoding="utf-8").strip()
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full Git commit ID")
    return commit, bool(git("status", "--porcelain", "--untracked-files=normal"))


def collect(root, target_os, configuration):
    if target_os not in ("windows", "linux") or configuration not in ("Debug", "Release"):
        raise ValueError("Unsupported build target/configuration")
    version = read_version(root)
    sources = source_inventory(root)
    migrations = migration_inventory(root, version)
    commit, dirty = git_state(root)
    identity = {key: version[key] for key in ("product", "version", "milestone", "upstream", "schemas",
                                            "api_version", "cache_format_version", "capabilities")}
    identity.update(identity_format=1, svn_base=version["upstream"]["subversion"],
                    target_os=target_os, target_arch="x86_64", configuration=configuration,
                    git_commit=commit, git_worktree_dirty=dirty,
                    source_manifest_sha256=object_sha256(sources),
                    migrations_sha256=object_sha256(migrations))
    return {"build_identity": identity, "sources": sources, "migrations": migrations}


def write_metadata(root, output, target_os, configuration):
    metadata = collect(root, target_os, configuration)
    identity = metadata["build_identity"]
    output.mkdir(parents=True, exist_ok=True)
    # C literals contain JSON escapes a second time. Never interpolate user or
    # filesystem strings directly into C source.
    prefix = encoded(identity).decode("ascii")[:-1]
    constants = {
        "PGVN_VERSION": identity["version"], "PGVN_MILESTONE": identity["milestone"],
        "PGVN_SVN_BASE": identity["svn_base"], "PGVN_CATALOG_VERSION": identity["schemas"]["catalog"],
        "PGVN_FS_VERSION": identity["schemas"]["fs"], "PGVN_API_VERSION": identity["api_version"],
        "PGVN_CACHE_FORMAT_VERSION": identity["cache_format_version"],
        "PGVN_CAPABILITIES_JSON": encoded(identity["capabilities"]).decode("ascii"),
        "PGVN_BUILD_JSON_PREFIX": prefix, "PGVN_BUILD_JSON_SIZE": len(prefix) + 512,
    }
    guard = "defined(_WIN32)" if target_os == "windows" else "defined(__linux__) && !defined(_WIN32)"
    lines = ["/* Generated from src/pgvn/version.json and the current inputs. Do not edit. */",
             "#ifndef PGVN_BUILD_INFO_GENERATED_H", "#define PGVN_BUILD_INFO_GENERATED_H",
             "#if !(" + guard + ")", '#error "PgVN build metadata does not match the compiler target OS"', "#endif",
             "#if !defined(__x86_64__) && !defined(_M_X64)",
             '#error "PgVN build metadata requires x86_64"', "#endif"]
    lines += ["#define " + name + " " + json.dumps(value, ensure_ascii=True) for name, value in constants.items()]
    lines.append("#endif\n")
    (output / "pgvn_build_info.h").write_bytes("\n".join(lines).encode("ascii"))
    (output / "build-inputs.json").write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("ascii") + b"\n")
    return metadata


def check_runtime(runtime, identity):
    for name, expected in identity.items():
        if name not in runtime or encoded(runtime[name]) != encoded(expected):
            raise ValueError("Runtime/build identity mismatch: " + name)
    for name, upstream in (("apr_runtime", "apr"), ("apu_runtime", "apr_util")):
        if runtime.get(name) != identity["upstream"][upstream]:
            raise ValueError("Runtime dependency version mismatch: " + upstream)
    major, minor = map(int, identity["upstream"]["postgresql"].split("."))
    if type(runtime.get("libpq_version")) is not int or runtime["libpq_version"] != major * 10000 + minor:
        raise ValueError("Runtime dependency version mismatch: libpq")


def check_pair(windows, linux):
    """Check provenance only. Functional/installation acceptance is separate."""
    for identity, target in ((windows, "windows"), (linux, "linux")):
        if identity.get("identity_format") != 1 or identity.get("product") != "PgVN":
            raise ValueError("Unsupported build identity")
        if identity.get("target_os") != target or identity.get("target_arch") != "x86_64":
            raise ValueError("Expected " + target + " x86_64 package")
        if identity.get("configuration") != "Release" or identity.get("git_worktree_dirty") is not False:
            raise ValueError("Paired packages require clean Release builds")
        if not re.fullmatch(r"[0-9a-f]{40}", identity.get("git_commit", "")):
            raise ValueError("Missing full Git commit ID")
        for name in ("source_manifest_sha256", "migrations_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", identity.get(name, "")):
                raise ValueError("Invalid " + name)
    required = {"identity_format", "product", "version", "milestone", "svn_base", "upstream", "schemas",
                "api_version", "cache_format_version", "capabilities", "target_os", "target_arch",
                "configuration", "git_commit", "git_worktree_dirty", "source_manifest_sha256", "migrations_sha256"}
    if set(windows) != required or set(linux) != required:
        raise ValueError("Incomplete or unknown build identity fields")
    for name in sorted(required - {"target_os"}):
        if encoded(windows[name]) != encoded(linux[name]):
            raise ValueError("Paired package mismatch: " + name)
