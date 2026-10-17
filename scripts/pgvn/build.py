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
from prepare_protocol import prepare_protocol
from prepare_dav import prepare_dav
from embed_migrations import embed
from embed_web import embed_web
from build_metadata import collect, write_metadata, check_runtime
from httpd_host import verify_sources,compile_resource,compile_host

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/pgvn"
RUNTIME = ROOT / "runtime/pgvn"
ARTIFACTS = ROOT / "artifacts/pgvn/build"
APACHE_MODULES=("mod_authz_core.so","mod_dav.so","mod_authn_core.so","mod_auth_basic.so","mod_authn_file.so",
    "mod_authz_user.so","mod_log_config.so","mod_headers.so","mod_ssl.so","mod_socache_shmcb.so",
    "mod_proxy.so","mod_proxy_http.so","mod_proxy_balancer.so","mod_slotmem_shm.so","mod_lbmethod_byrequests.so")


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
    parser.add_argument("--config", type=Path)
    parser.add_argument("--target", choices=("windows", "linux"), default="windows" if os.name == "nt" else "linux")
    parser.add_argument("--metadata-only", action="store_true", help="prepare shared version/source inputs without SDKs or product compilation")
    parser.add_argument("--configuration", choices=("Debug", "Release"), default="Debug")
    parser.add_argument("--components", choices=("all", "fs", "credentials"), default="all")
    parser.add_argument("--linux-sdk", type=Path, help="verified SDK from prepare_linux_sdk.py")
    parser.add_argument("--fs-content-transfer", choices=("binary", "hex"), default="binary",
                        help="FS experiment control: binary bytea or legacy hex baseline")
    parser.add_argument("--sanitize", action="store_true", help="ASan/UBSan for Linux development components")
    args = parser.parse_args()
    if args.metadata_only:
        output = BUILD / "metadata" / args.target
        metadata = write_metadata(ROOT, output, args.target, args.configuration)
        print(json.dumps({"output": str(output), "build_identity": metadata["build_identity"], "products_built": False}))
        return
    if args.target == "linux" and args.components in ("fs", "credentials"):
        if args.linux_sdk is None:
            parser.error("--linux-sdk is required for native FS builds")
        from build_linux import build_component
        print(json.dumps(build_component(args.linux_sdk, args.configuration, args.components, args.fs_content_transfer, args.sanitize)))
        return
    if args.components != "all":
        parser.error("--components fs/credentials currently applies to Linux only")
    if args.target != "windows" or os.name != "nt":
        parser.error("Complete native Linux hosts are not implemented yet (PX-L01–PX-L06); use --components fs --linux-sdk PATH or --metadata-only")
    if args.config is None:
        parser.error("--config is required for product compilation")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    required = ("toolchain", "subversion", "apr_include", "apu_include", "svn_bin", "apache", "postgres")
    paths = {k: Path(config[k]).resolve(strict=True) for k in required}
    tools, svn = paths["toolchain"], paths["subversion"] / "subversion"
    apr, apu = paths["apr_include"], paths["apu_include"]
    svn_bin, apache, pg = paths["svn_bin"], paths["apache"], paths["postgres"]
    paths['apache_source']=Path(config.get('apache_source',ROOT/'vendor/httpd-2.4.68')).resolve(strict=True)
    host_metadata=verify_sources(ROOT,paths['apache_source'],apache)
    for path in (BUILD / "generated", BUILD / "imports", BUILD / "objects",
                 RUNTIME / "bin", RUNTIME / "apache/bin", RUNTIME / "apache/modules",
                 RUNTIME / "apache/logs", ARTIFACTS):
        path.mkdir(parents=True, exist_ok=True)
    metadata = write_metadata(ROOT, BUILD / "generated", args.target, args.configuration)
    identity = metadata["build_identity"]
    if identity["svn_base"] not in (svn / "include/svn_version.h").read_text(encoding="utf-8"):
        # SVN_VER_NUMBER may be assembled from individual components.
        version = (svn / "include/svn_version.h").read_text(encoding="utf-8")
        for key, value in zip(("MAJOR", "MINOR", "PATCH"), identity["svn_base"].split(".")):
            if not re.search(r"#define SVN_VER_" + key + r"\s+" + value + r"\b", version):
                raise RuntimeError("Subversion headers must match src/pgvn/version.json")
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
            "libsvn_ra-1.dll", "libsvn_diff-1.dll", "libsvn_fs_util-1.dll")] + [apache / "bin/libhttpd.dll",apache / "modules/mod_dav.so"]
    for dll in dlls:
        libraries[dll.name], exports[dll.name] = import_library(tools, dll)
    # The two hosts must use the same APR data ABI during this pinned M0 build.
    for name in ("libapr-1.dll", "libaprutil-1.dll"):
        if digest(svn_bin / name) != digest(apache / "bin" / name):
            raise RuntimeError(f"SVN and Apache have different {name}; separate ABI builds are required")
    repository_source = BUILD / "upstream/libsvn_repos"
    patched = prepare(svn / "libsvn_repos", repository_source,
                      ROOT / "patches/pgvn/subversion-1.14.5/repos.json")
    protocol_patched = prepare_protocol(svn,BUILD / "upstream",
                      ROOT / "patches/pgvn/subversion-1.14.5/protocol.json")
    dav_source=BUILD / "upstream/dav"
    dav_patched=prepare_dav(svn,dav_source,ROOT / "patches/pgvn/subversion-1.14.5/dav.json")
    legacy_spec = importlib.util.spec_from_file_location("pgfs_legacy_build", ROOT / "scripts/build.py")
    legacy = importlib.util.module_from_spec(legacy_spec)
    legacy_spec.loader.exec_module(legacy)
    legacy.ROOT, legacy.BUILD, legacy.SVN = ROOT, BUILD, svn
    legacy.TOOLS, legacy.RUNTIME, legacy.SVN_BIN = tools, RUNTIME / "legacy/bin", svn_bin
    legacy.generate_stubs()
    embed(ROOT,BUILD / "generated/pgvn_migrations.h")
    embed_web(ROOT,BUILD / "generated")
    includes = [BUILD / "generated", ROOT / "src/pgvn/core", ROOT / "src/pgvn/monitor",
                ROOT / "src/pgvn/serve", BUILD / "upstream/svnserve",
                ROOT / "src/libsvn_fs_pg", repository_source,
                svn / "include", svn / "libsvn_fs", apr, apu, pg / "include", apache / "include"]
    options = ["-std=c17", "-O0", "-g"] if args.configuration == "Debug" else ["-std=c17", "-O2", "-DNDEBUG"]
    options += ["-DWIN32", "-D_M_X64", "-D_WIN32_WINNT=0x0601", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    for path in includes:
        options += ["-I", path]
    compiler = tools / "clang.exe"
    utf8_resource=compile_resource(ROOT,BUILD,tools,run)
    objects = []
    for source in sorted((ROOT / "src/pgvn/core").glob("*.c")) + [ROOT / "src/libsvn_fs_pg/pgfs.c", ROOT / "src/pgvn/platform/credentials_win.c"]:
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
        "libsvn_diff-1.dll", "libsvn_delta-1.dll", "libsvn_subr-1.dll", "libaprutil-1.dll", "libapr-1.dll")], pg / "lib/libpq.lib", "-lbcrypt", "-ladvapi32"]
    products = [RUNTIME / "bin/pgvnserve.exe", RUNTIME / "bin/pgvnadmin.exe",
                RUNTIME / "apache/modules/mod_dav_pgvn.so", RUNTIME / "apache/modules/mod_authz_pgvn.so"]
    protocol_objects = []
    protocol_sources = [BUILD / "upstream/svnserve" / name for name in ("serve.c", "logger.c", "log-escape.c")]
    protocol_sources += [BUILD / "upstream/libsvn_ra_svn" / name for name in
                         ("marshal.c", "streams.c", "editorp.c", "cram.c", "deprecated.c")]
    for source in protocol_sources:
        output = BUILD / "objects" / ("protocol-" + source.stem + ".o")
        run([compiler, *[o for o in options if o != "-Werror"], "-Wno-deprecated-declarations", "-Wno-missing-field-initializers",
             "-c", source, "-o", output])
        protocol_objects.append(output)
    run([compiler, *options, "-municode", *sorted((ROOT / "src/pgvn/serve").glob("*.c")),
         *sorted((ROOT / "src/pgvn/monitor").glob("*.c")), BUILD / "generated/pgvn_web.c", *protocol_objects, *core_links, "-lhttpapi", "-lws2_32", "-lpsapi", "-ladvapi32",
         utf8_resource,"-Wl,--no-insert-timestamp", "-o", products[0]])
    run([compiler, *options, "-municode", *sorted((ROOT / "src/pgvn/admin").glob("*.c")), *core_links, "-lbcrypt",
         utf8_resource,"-Wl,--no-insert-timestamp", "-o", products[1]])
    dav_options=[o for o in options if o!='-Werror']
    dav_options[:0]=["-I",dav_source / "include","-I",dav_source / "mod_dav_svn","-I",ROOT / "src/pgvn/dav"]
    dav_options += ["-include",dav_source / "pgvn_dav_namespace.h","-DPGVN_REAL_DAV",
                    "-Wno-deprecated-declarations","-Wno-missing-field-initializers"]
    dav_objects=[]
    for source in sorted((dav_source / "mod_dav_svn").rglob("*.c")):
        output=BUILD / "objects" / ("dav-"+source.relative_to(dav_source / "mod_dav_svn").as_posix().replace('/','-')+'.o')
        run([compiler,*dav_options,"-DPGVN_DAV_BUILD","-c",source,"-o",output]);dav_objects.append(output)
    run([compiler,*dav_options,"-DPGVN_DAV_BUILD","-shared",ROOT / "src/pgvn/dav/adapter.c",
         ROOT / "src/pgvn/dav/status.c",ROOT / "src/pgvn/dav/bootstrap.c",*dav_objects,*core_links,libraries["libhttpd.dll"],libraries["mod_dav.so"],"-lws2_32","-lpsapi",
         "-Wl,--no-insert-timestamp","-o",products[2]])
    dav_import,dav_exports=import_library(tools,products[2])
    run([compiler,*dav_options,"-DPGVN_AUTHZ_BOOTSTRAP","-shared",dav_source / "mod_authz_svn/mod_authz_svn.c",
         ROOT / "src/pgvn/dav/bootstrap.c",dav_import,*core_links,libraries["libhttpd.dll"],libraries["mod_dav.so"],
         "-Wl,--no-insert-timestamp","-o",products[3]])
    probe = ROOT / "tests/pgvn/core_probe.c"
    dav_probe = ROOT / "tests/pgvn/dav_state_probe.c"
    if dav_probe.exists():
        run([compiler, *options, "-municode", dav_probe, *core_links, "-Wl,--no-insert-timestamp",
             utf8_resource,"-o", RUNTIME / "bin/dav_state_probe.exe"])
    registry_probe = ROOT / "tests/pgvn/dav_registry_probe.c"
    if registry_probe.exists():
        run([compiler, *options, "-municode", registry_probe, *core_links, "-Wl,--no-insert-timestamp",
             utf8_resource,"-o", RUNTIME / "bin/dav_registry_probe.exe"])
    if probe.exists():
        run([compiler, *options, "-municode", probe, *core_links, "-Wl,--no-insert-timestamp",
             utf8_resource,"-o", RUNTIME / "bin/core_probe.exe"])
    hook_probe = ROOT / "tests/pgvn/hook_probe.c"
    monitor_probe = ROOT / "tests/pgvn/monitor_probe.c"
    if monitor_probe.exists():
        run([compiler, *options, monitor_probe, ROOT / "src/pgvn/monitor/data.c", BUILD / "generated/pgvn_web.c",
             *core_links,utf8_resource,"-lws2_32", "-lpsapi", "-Wl,--no-insert-timestamp", "-o", RUNTIME / "bin/monitor_probe.exe"])
    if hook_probe.exists():
        run([compiler, *options, "-municode", hook_probe,utf8_resource,"-Wl,--no-insert-timestamp", "-o", RUNTIME / "bin/hook_probe.exe"])
    # Copy runtime dependencies into the isolated output; input installations are read-only.
    for source, target in ((svn_bin, RUNTIME / "bin"), (apache / "bin", RUNTIME / "apache/bin")):
        for item in source.iterdir():
            if item.suffix.lower() in (".dll", ".exe"):
                shutil.copy2(item, target / item.name)
        for item in (pg / "bin").glob("*.dll"):
            shutil.copy2(item, target / item.name)
    # Core Windows MPM is built into this distribution. Other test modules are copied explicitly.
    for name in APACHE_MODULES:
        shutil.copy2(apache / "modules" / name, RUNTIME / "apache/modules" / name)
    host=RUNTIME/'apache/bin/pgvnhttpd.exe'
    compile_host(ROOT,paths['apache_source'],BUILD,tools,options,libraries,utf8_resource,host,run)
    host_runtime=json.loads(run([host,'--pgvn-runtime']))
    if host_runtime!={'product':'PgVNHTTPD','apache_base':identity['upstream']['httpd'],'process_code_page':65001,'build_identity':identity}:
        raise RuntimeError('Unexpected source-built HTTP host runtime')
    host_metadata.update(product=host.relative_to(ROOT).as_posix(),runtime=host_runtime,
        process_manifest_sha256=digest(ROOT/'src/pgvn/windows/utf8.manifest'),
        sdk_host_sha256=digest(apache/'bin/httpd.exe'),sdk_core_sha256=digest(apache/'bin/libhttpd.dll'))
    products.append(host)
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
    runtime_build_info = {}
    for name in ("pgvnserve", "pgvnadmin"):
        diagnosis = json.loads(run([RUNTIME / "bin" / (name + ".exe"), "--version"]))
        check_runtime(diagnosis, identity)
        runtime_build_info[name] = diagnosis
    if collect(ROOT, args.target, args.configuration) != metadata:
        raise RuntimeError("Build inputs changed during compilation; rebuild before generating evidence")
    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "milestone": identity["milestone"], "version": identity["version"], "configuration": args.configuration,
        "build_identity": identity, "migrations": metadata["migrations"], "runtime_build_info": runtime_build_info,
        "capabilities": identity["capabilities"],
        "compiler": run([compiler, "--version"]).strip(),
        "compiler_sha256": digest(compiler), "flags": [str(o) for o in options],
        "inputs": {k: str(v) for k, v in paths.items()},
        "windows_ctype_array_abi": array_abi,
        "upstream_patches": {"specification_sha256": digest(ROOT / "patches/pgvn/subversion-1.14.5/repos.json"),
                             "applied_sha256": digest(BUILD / "upstream/applied-repos.patch"), "outputs": patched},
        "protocol_patches": {"specification_sha256": digest(ROOT / "patches/pgvn/subversion-1.14.5/protocol.json"),
                             "applied_sha256": digest(BUILD / "upstream/applied-protocol.patch"), "outputs": protocol_patched},
        "dav_patches": {"specification_sha256": digest(ROOT / "patches/pgvn/subversion-1.14.5/dav.json"),
                        "applied_sha256": digest(dav_source / "applied-dav.patch"),"outputs":dav_patched,
                        "namespace_sha256":digest(dav_source / "pgvn_dav_namespace.h"),
                        "provider":"pgvn","public_exports":dav_exports},
        "sources": metadata["sources"],
        "upstream": {"subversion": inventory(paths["subversion"], {".c", ".h", ".inc", ".hw"}),
                     "apr": inventory(apr), "apu": inventory(apu), "apache_include": inventory(apache / "include")},
        "sdk_binaries": {str(d): digest(d) for d in dlls + [pg / "lib/libpq.lib", pg / "bin/libpq.dll"]},
        "products": binary_evidence,
        "core_archive_sha256":digest(static_core),
        "apache_host":host_metadata,
        "git_base_commit": identity["git_commit"],
        "git_worktree_dirty": identity["git_worktree_dirty"],
        "runtime_dependencies": {"bin/" + k: v for k, v in inventory(RUNTIME / "bin").items()},
        "apache_dependencies": {**{"apache/bin/"+k:v for k,v in inventory(RUNTIME / "apache/bin",{".exe",".dll"}).items()},
                                **{"apache/modules/"+name:digest(RUNTIME / "apache/modules" / name) for name in APACHE_MODULES}},
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
    build_id = hashlib.sha256(json.dumps({key: manifest[key] for key in (
        "build_identity", "sources", "upstream_patches", "protocol_patches", "dav_patches", "upstream", "compiler_sha256", "sdk_binaries", "configuration")},
        sort_keys=True).encode()).hexdigest()[:16]
    archive = ARTIFACTS / (manifest["milestone"] + "-" + build_id + ".json")
    if not archive.exists():
        shutil.copy2(manifest_path, archive)
    print(json.dumps({"manifest": str(manifest_path), "configuration": args.configuration,
                      "products": [str(p) for p in products], "private_api_audit": manifest["private_api_audit"]}))


if __name__ == "__main__":
    main()
