"""Build pinned native PgVN products without modifying any input SDK.

Python is a build/test dependency only. Runtime products use C/APR/libpq.
Local paths come from --config, never from a production installation default.
"""
from pathlib import Path
import argparse
import ctypes
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
from prepare_upstream import prepare

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/pgvn"
RUNTIME = ROOT / "runtime/pgvn"
ARTIFACTS = ROOT / "artifacts/pgvn/build"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(base, suffixes=None):
    return {p.relative_to(base).as_posix(): digest(p)
            for p in sorted(base.rglob("*")) if p.is_file()
            and (suffixes is None or p.suffix in suffixes)}


def run(args):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=180)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with (ARTIFACTS / "commands.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"args": [str(a) for a in args], "exit_code": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr}) + "\n")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n"
                           f"{result.stdout}\n{result.stderr}")
    return result.stdout


def import_library(tools, dll):
    exports = run([tools / "llvm-readobj.exe", "--coff-exports", dll])
    image = dll.read_bytes()
    pe = struct.unpack_from("<I", image, 0x3c)[0]
    count = struct.unpack_from("<H", image, pe + 6)[0]
    size = struct.unpack_from("<H", image, pe + 20)[0]
    sections = []
    for i in range(count):
        offset = pe + 24 + size + 40 * i
        virtual, address, raw = struct.unpack_from("<III", image, offset + 8)
        flags = struct.unpack_from("<I", image, offset + 36)[0]
        sections.append((address, address + max(virtual, raw), flags))
    declarations = []
    for block in re.findall(r"Export \{(.*?)\}", exports, re.S):
        name = re.search(r"Name: (\S+)", block).group(1)
        address = int(re.search(r"RVA: (0x[0-9A-Fa-f]+)", block).group(1), 16)
        code = any(start <= address < end and flags & 0x20000000
                   for start, end, flags in sections)
        declarations.append(name + ("" if code else " DATA"))
    if not declarations:
        raise RuntimeError(f"No exports found in {dll}")
    output = BUILD / "imports" / (dll.stem + ".def")
    output.write_text("LIBRARY " + dll.name + "\nEXPORTS\n"
                      + "\n".join(declarations) + "\n", encoding="ascii")
    library = output.with_suffix(".a")
    run([tools / "llvm-dlltool.exe", "-m", "i386:x86-64", "-d", output, "-l", library])
    return library, [d.removesuffix(" DATA") for d in declarations]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--configuration", choices=("Debug", "Release"), default="Debug")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    required = ("toolchain", "subversion", "apr_include", "apu_include", "svn_bin", "apache", "postgres")
    paths = {k: Path(config[k]).resolve(strict=True) for k in required}
    tools, svn = paths["toolchain"], paths["subversion"] / "subversion"
    apr, apu = paths["apr_include"], paths["apu_include"]
    svn_bin, apache, pg = paths["svn_bin"], paths["apache"], paths["postgres"]
    for path in (BUILD / "generated", BUILD / "imports", BUILD / "objects",
                 RUNTIME / "bin", RUNTIME / "apache/bin", RUNTIME / "apache/modules",
                 RUNTIME / "apache/logs", ARTIFACTS):
        path.mkdir(parents=True, exist_ok=True)
    if "1.14.5" not in (svn / "include/svn_version.h").read_text(encoding="utf-8"):
        # SVN_VER_NUMBER may be assembled from individual components.
        version = (svn / "include/svn_version.h").read_text(encoding="utf-8")
        for key, value in (("MAJOR", "1"), ("MINOR", "14"), ("PATCH", "5")):
            if not re.search(r"#define SVN_VER_" + key + r"\s+" + value + r"\b", version):
                raise RuntimeError("Subversion headers must be pinned to 1.14.5")
    shutil.copy2(apr / "apr.hw", BUILD / "generated/apr.h")
    shutil.copy2(apu / "apu.hw", BUILD / "generated/apu.h")
    shutil.copy2(svn / "svn_private_config.hw", BUILD / "generated/svn_private_config.h")
    # This SDK was built with MSVC. Its os.h/apr.h already describe Windows;
    # ap_config.h additionally includes an autoconf header for any MinGW
    # consumer. Supply an empty, explicit consumer shim, not Unix features.
    (BUILD / "generated/ap_config_auto.h").write_text(
        "/* MSVC Windows Apache SDK consumed with LLVM-MinGW. */\n"
        "/* Platform features come from the pinned os.h and APR headers. */\n",
        encoding="ascii")
    with os.add_dll_directory(str(svn_bin)):
        subr = ctypes.CDLL(str(svn_bin / "libsvn_subr-1.dll"))
        table = (ctypes.c_uint32 * 256).in_dll(subr, "svn_ctype_table")
        array_abi = table[0] == 0x81 and table[ord("A")] == 0xc8 and table[ord("0")] == 0x84
    ctype = (svn / "include/svn_ctype.h").read_text(encoding="utf-8")
    if array_abi:
        declaration = "extern const apr_uint32_t *const svn_ctype_table;"
        if declaration not in ctype:
            raise RuntimeError("Pinned ctype declaration changed")
        ctype = ctype.replace(declaration, "extern const apr_uint32_t svn_ctype_table[];")
    (BUILD / "generated/svn_ctype.h").write_text(ctype, encoding="utf-8")

    libraries, exports = {}, {}
    dlls = [svn_bin / name for name in ("libapr-1.dll", "libaprutil-1.dll",
            "libsvn_subr-1.dll", "libsvn_delta-1.dll", "libsvn_fs-1.dll", "libsvn_repos-1.dll",
            "libsvn_ra-1.dll", "libsvn_diff-1.dll", "libsvn_fs_util-1.dll")] + [apache / "bin/libhttpd.dll"]
    for dll in dlls:
        libraries[dll.name], exports[dll.name] = import_library(tools, dll)
    # The two hosts must use the same APR data ABI during this pinned M0 build.
    for name in ("libapr-1.dll", "libaprutil-1.dll"):
        if digest(svn_bin / name) != digest(apache / "bin" / name):
            raise RuntimeError(f"SVN and Apache have different {name}; separate ABI builds are required")
    repository_source = BUILD / "upstream/libsvn_repos"
    patched = prepare(svn / "libsvn_repos", repository_source,
                      ROOT / "patches/pgvn/subversion-1.14.5/repos.json")
    legacy_spec = importlib.util.spec_from_file_location("pgfs_legacy_build", ROOT / "scripts/build.py")
    legacy = importlib.util.module_from_spec(legacy_spec)
    legacy_spec.loader.exec_module(legacy)
    legacy.ROOT, legacy.BUILD, legacy.SVN = ROOT, BUILD, svn
    legacy.TOOLS, legacy.RUNTIME, legacy.SVN_BIN = tools, RUNTIME / "legacy/bin", svn_bin
    legacy.generate_stubs()
    includes = [BUILD / "generated", ROOT / "src/pgvn/core", ROOT / "src/pgvn/monitor",
                ROOT / "src/libsvn_fs_pg", repository_source,
                svn / "include", svn / "libsvn_fs", apr, apu, pg / "include", apache / "include"]
    options = ["-std=c17", "-O0", "-g"] if args.configuration == "Debug" else ["-std=c17", "-O2", "-DNDEBUG"]
    options += ["-DWIN32", "-D_M_X64", "-D_WIN32_WINNT=0x0601", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    for path in includes:
        options += ["-I", path]
    compiler = tools / "clang.exe"
    objects = []
    for source in sorted((ROOT / "src/pgvn/core").glob("*.c")) + [ROOT / "src/libsvn_fs_pg/pgfs.c"]:
        output = BUILD / "objects" / ("core-" + source.stem + ".o")
        run([compiler, *options, "-DPGVN_STATIC", "-c", source, "-o", output])
        objects.append(output)
    for source in sorted(repository_source.glob("*.c")):
        output = BUILD / "objects" / ("repos-" + source.stem + ".o")
        run([compiler, *[o for o in options if o != "-Werror"], "-Wno-deprecated-declarations", "-Wno-missing-field-initializers",
             "-c", source, "-o", output])
        objects.append(output)
    static_core = BUILD / "libpgvn_core.a"
    if static_core.exists():
        static_core.unlink()
    run([tools / "llvm-ar.exe", "rcs", static_core, *objects])
    core_links = [static_core, *[libraries[name] for name in ("libsvn_fs-1.dll", "libsvn_fs_util-1.dll",
        "libsvn_diff-1.dll", "libsvn_delta-1.dll", "libsvn_subr-1.dll", "libaprutil-1.dll", "libapr-1.dll")], pg / "lib/libpq.lib"]
    products = [RUNTIME / "bin/pgvnserve.exe", RUNTIME / "bin/pgvnadmin.exe",
                RUNTIME / "apache/modules/mod_dav_pgvn.so", RUNTIME / "apache/modules/mod_authz_pgvn.so"]
    run([compiler, *options, "-municode", ROOT / "src/pgvn/serve/main.c",
         ROOT / "src/pgvn/monitor/http_win.c", *core_links, "-lhttpapi", "-ladvapi32",
         "-Wl,--no-insert-timestamp", "-o", products[0]])
    run([compiler, *options, ROOT / "src/pgvn/admin/main.c", *core_links,
         "-Wl,--no-insert-timestamp", "-o", products[1]])
    for product, define in ((products[2], []), (products[3], ["-DPGVN_AUTHZ_BOOTSTRAP"])):
        run([compiler, *options, "-shared", *define, ROOT / "src/pgvn/dav/bootstrap.c",
             *core_links, libraries["libhttpd.dll"], "-Wl,--no-insert-timestamp", "-o", product])
    probe = ROOT / "tests/pgvn/core_probe.c"
    if probe.exists():
        run([compiler, *options, "-municode", probe, *core_links, "-Wl,--no-insert-timestamp",
             "-o", RUNTIME / "bin/core_probe.exe"])
    # Copy runtime dependencies into the isolated output; input installations are read-only.
    for source, target in ((svn_bin, RUNTIME / "bin"), (apache / "bin", RUNTIME / "apache/bin")):
        for item in source.iterdir():
            if item.suffix.lower() in (".dll", ".exe"):
                shutil.copy2(item, target / item.name)
        for item in (pg / "bin").glob("*.dll"):
            shutil.copy2(item, target / item.name)
    # Core Windows MPM is built into this distribution. Other test modules are copied explicitly.
    for name in ("mod_authz_core.so",):
        shutil.copy2(apache / "modules" / name, RUNTIME / "apache/modules" / name)
    legacy.RUNTIME.mkdir(parents=True, exist_ok=True)
    for item in (RUNTIME / "bin").glob("*"):
        if item.suffix.lower() in (".dll", ".exe") and not item.name.startswith(("pgvn", "core_probe")):
            shutil.copy2(item, legacy.RUNTIME / item.name)
    plugin_imports = [libraries[name] for name in ("libapr-1.dll", "libaprutil-1.dll", "libsvn_subr-1.dll", "libsvn_delta-1.dll")]
    run([compiler, *options, "-shared", ROOT / "src/libsvn_fs_pg/pgfs.c", *plugin_imports,
         pg / "lib/libpq.lib", "-Wl,--no-insert-timestamp", "-o", legacy.RUNTIME / "libsvn_fs_pg-1.dll"])
    legacy.build_dynamic_loader(includes, plugin_imports + [libraries["libsvn_fs_util-1.dll"]])
    legacy_fs, _ = import_library(tools, legacy.RUNTIME / "libsvn_fs-1.dll")
    run([compiler, *options, ROOT / "tests/fs_probe.c", *plugin_imports, legacy_fs,
         "-Wl,--no-insert-timestamp", "-o", legacy.RUNTIME / "fs_probe.exe"])
    run([compiler, *[o for o in options if o != "-Werror"], "-Wno-deprecated-declarations",
         svn / "tests/cmdline/lock-helper.c", *plugin_imports, legacy_fs, libraries["libsvn_repos-1.dll"],
         "-Wl,--no-insert-timestamp", "-o", legacy.RUNTIME / "lock-helper.exe"])
    binary_evidence = {}
    for product in products:
        binary_evidence[product.relative_to(ROOT).as_posix()] = {
            "sha256": digest(product),
            "pe": run([tools / "llvm-readobj.exe", "--file-headers", "--coff-imports", "--coff-exports", product])}
    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "milestone": "M1", "configuration": args.configuration,
        "capabilities": {"repository_by_id": True, "svn_protocol": False, "dav_protocol": False, "monitor": "bootstrap"},
        "compiler": run([compiler, "--version"]).strip(),
        "compiler_sha256": digest(compiler), "flags": [str(o) for o in options],
        "inputs": {k: str(v) for k, v in paths.items()},
        "windows_ctype_array_abi": array_abi,
        "upstream_patches": {"specification_sha256": digest(ROOT / "patches/pgvn/subversion-1.14.5/repos.json"),
                             "applied_sha256": digest(BUILD / "upstream/applied-repos.patch"), "outputs": patched},
        "sources": {**{"src/" + k: v for k, v in inventory(ROOT / "src").items()},
                    **{"sql/" + k: v for k, v in inventory(ROOT / "sql").items()},
                    **{"scripts/pgvn/" + k: v for k, v in inventory(ROOT / "scripts/pgvn", {".py", ".ps1"}).items()}},
        "upstream": {"subversion": inventory(paths["subversion"], {".c", ".h", ".inc", ".hw"}),
                     "apr": inventory(apr), "apu": inventory(apu), "apache_include": inventory(apache / "include")},
        "sdk_binaries": {str(d): digest(d) for d in dlls + [pg / "lib/libpq.lib", pg / "bin/libpq.dll"]},
        "products": binary_evidence,
        "runtime_dependencies": {"bin/" + k: v for k, v in inventory(RUNTIME / "bin").items()},
        "private_api_audit": {
            "ra_svn_exported": sorted(e for e in exports["libsvn_ra-1.dll"] if e.startswith("svn_ra_svn")),
            "ra_svn_requires_source_build": "svn_ra_svn__read_tuple" not in exports["libsvn_ra-1.dll"],
            "fs_private_new_exported": "svn_fs_new" in exports["libsvn_fs-1.dll"],
            "repository_private_constructor_exported": "create_svn_repos_t" in exports["libsvn_repos-1.dll"],
            "data_imports_marked": True,
        },
    }
    manifest_path = ARTIFACTS / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "configuration": args.configuration,
                      "products": [str(p) for p in products], "private_api_audit": manifest["private_api_audit"]}))


if __name__ == "__main__":
    main()
