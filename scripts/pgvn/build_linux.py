"""Build the Linux platform library, monitor, and ELF checks.

The Windows product build stays in build.py. This target does not claim a
pinned Subversion server binary; it records whether that SDK is present.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/pgvn-linux"
RUNTIME = ROOT / "runtime/pgvn-linux"
ARTIFACTS = ROOT / "artifacts/pgvn/build-linux"
PLATFORM = [
    "crypto.c", "pathwalk.c", "keyfile.c", "spawn.c", "lru.c", "monitor_auth.c",
    "http_dispatch.c", "http_linux.c",
]
ALLOWED_NEEDED = (
    "libc.so", "libdl.so", "libm.so", "libpthread.so", "librt.so", "libz.so",
    "libssl.so", "libcrypto.so", "libevent", "ld-linux",
)


def run(args, cwd=ROOT):
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with (ARTIFACTS / "commands.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"args": [str(a) for a in args], "exit_code": result.returncode,
                              "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}) + "\n")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def needed_libraries(binary):
    text = run(["readelf", "-d", binary])
    names = []
    for line in text.splitlines():
        if "(NEEDED)" in line and "[" in line and "]" in line:
            names.append(line.split("[", 1)[1].split("]", 1)[0])
    return names


def rpath_values(binary):
    text = run(["readelf", "-d", binary])
    values = []
    for line in text.splitlines():
        if "(RPATH)" in line or "(RUNPATH)" in line:
            values.append(line.split("[", 1)[1].split("]", 1)[0])
    return values


def assert_elf(binary):
    for name in needed_libraries(binary):
        if any(token in name for token in ("svn", "apr", "pq", "httpd")):
            raise RuntimeError(f"{binary.name} links an unexpected library: {name}")
        if not any(name.startswith(prefix) for prefix in ALLOWED_NEEDED):
            raise RuntimeError(f"{binary.name} links an unapproved SONAME: {name}")
    for value in rpath_values(binary):
        if value not in ("$ORIGIN", "${ORIGIN}"):
            raise RuntimeError(f"{binary.name} has an unexpected RPATH: {value}")


def sdk_status():
    header = Path("/usr/include/subversion-1/svn_version.h")
    status = {"svn_header": str(header), "present": header.is_file(), "pinned": "1.14.5", "matches": False}
    if header.is_file():
        text = header.read_text(encoding="utf-8", errors="replace")
        status["matches"] = "1.14.5" in text
    strict = os.environ.get("PGVN_STRICT_SDK") == "1"
    if strict and not status["matches"]:
        raise RuntimeError("PGVN_STRICT_SDK=1 requires the pinned Subversion 1.14.5 headers")
    return status


def build_linux(configuration):
    compiler = os.environ.get("CC") or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        raise RuntimeError("gcc or clang is required")
    if not shutil.which("readelf"):
        raise RuntimeError("readelf is required to check ELF dependencies")
    openssl = run(["pkg-config", "--cflags", "--libs", "openssl"]).split()
    libevent = run(["pkg-config", "--cflags", "--libs", "libevent"]).split()
    for path in (BUILD / "objects", RUNTIME / "bin", RUNTIME / "share/web", ARTIFACTS):
        path.mkdir(parents=True, exist_ok=True)
    flags = ["-std=c17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-D_GNU_SOURCE",
             "-I", ROOT / "src/pgvn/platform", "-I", ROOT / "src/pgvn/core"]
    flags += ["-O0", "-g"] if configuration == "Debug" else ["-O2", "-DNDEBUG"]
    objects = []
    sources = [ROOT / "src/pgvn/platform" / name for name in PLATFORM]
    sources.append(ROOT / "src/pgvn/core/hook_name.c")
    for source in sources:
        output = BUILD / "objects" / (source.stem + ".o")
        run([compiler, *flags, "-c", source, "-o", output])
        objects.append(output)
    archive = BUILD / "libpgvn_platform.a"
    if archive.exists():
        archive.unlink()
    run(["ar", "rcs", archive, *objects])
    info = RUNTIME / "bin/pgvn-platform-info"
    monitor = RUNTIME / "bin/pgvn-monitor"
    probe = RUNTIME / "bin/platform_test"
    run([compiler, *flags, ROOT / "src/pgvn/platform/platform_info.c", archive, *openssl, "-o", info])
    run([compiler, *flags, ROOT / "src/pgvn/platform/monitor_main.c", archive, *openssl, *libevent, "-o", monitor])
    run([compiler, *flags, "-pthread", ROOT / "tests/pgvn/linux/test_platform.c", archive, *openssl, "-o", probe])
    for binary in (info, monitor, probe):
        assert_elf(binary)
    web = ROOT / "web/pgvn-monitor"
    for name in ("index.html", "monitor.css", "monitor.js"):
        shutil.copy2(web / name, RUNTIME / "share/web" / name)
    info_text = run([info]).strip()
    manifest = {
        "target": "linux",
        "arch": "x86_64",
        "configuration": configuration,
        "compiler": run([compiler, "--version"]).splitlines()[0],
        "products": {
            info.name: digest(info),
            monitor.name: digest(monitor),
        },
        "platform_info": json.loads(info_text),
        "sdk": sdk_status(),
        "elf": {binary.name: needed_libraries(binary) for binary in (info, monitor, probe)},
    }
    (ARTIFACTS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(ARTIFACTS / "manifest.json"), "products": [str(info), str(monitor)]}))
