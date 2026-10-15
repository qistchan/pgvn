"""Build the native PgFS DLL against the pinned SVN/APR ABI on Windows."""
from pathlib import Path
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "toolchains/llvm-mingw-20261006-ucrt-x86_64/bin"
SVN = ROOT / "vendor/subversion-1.14.5/subversion"
APR = ROOT / "vendor/apr-1.7.6/include"
APU = ROOT / "vendor/apr-util-1.6.3/include"
PG = Path(os.environ.get("PGFS_POSTGRES_HOME", r"C:\Program Files\PostgreSQL\18"))
SVN_BIN = Path(os.environ.get("PGFS_SVN_BIN", r"C:\Polarion\bundled\svn\bin"))
BUILD = ROOT / "build"
RUNTIME = ROOT / "runtime/svn/bin"


def run(args):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, text=True,
                            encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n"
                           f"{result.stdout}\n{result.stderr}")
    return result.stdout


def generate_stubs():
    header = (SVN / "libsvn_fs/fs-loader.h").read_text(encoding="utf-8")
    source = "\n".join(p.read_text(encoding="utf-8") for p in
                       sorted((ROOT / "src/libsvn_fs_pg").glob("*")) if p.suffix in (".c", ".inc", ".h"))
    stripped = re.sub(r"/\*.*?\*/", "", header, flags=re.S)
    structures = ["fs_library_vtable_t", "fs_vtable_t", "txn_vtable_t",
                  "root_vtable_t", "history_vtable_t"]
    out = ["/* Generated from the pinned Subversion ABI. Do not edit. */"]
    inventory = []
    for struct in structures:
        body = re.search(r"typedef struct " + struct + r"\s*\{(.*?)\}\s*" + struct,
                         stripped, re.S).group(1)
        callbacks = re.findall(r"svn_error_t\s*\*\s*\(\*(\w+)\)\s*\((.*?)\);",
                               body, re.S)
        tag = struct.removesuffix("_vtable_t")
        out.append(f"static {struct} pgfs_{tag}_vtable;")
        for field, args in callbacks:
            out.append(f"static svn_error_t *pgfs_stub_{tag}_{field}({args})"
                       f' {{ return pgfs_unsupported("{tag}.{field}"); }}')
            implemented = re.search(r"pgfs_" + tag + r"_vtable\." + field
                                    + r"\s*=\s*(pgfs_\w+)\s*;", source)
            inventory.append({"vtable": struct, "callback": field,
                              "implementation": implemented.group(1) if implemented else None,
                              "status": "implemented" if implemented else
                              "emulated" if field == "report_changes" and struct == "root_vtable_t" else "unsupported"})
        out.append(f"static void pgfs_initialize_{tag}_stubs(void) {{")
        for field, _ in callbacks:
            out.append(f"  pgfs_{tag}_vtable.{field} = pgfs_stub_{tag}_{field};")
        out.append("}")
    (BUILD / "generated/pgfs_stubs.h").write_text("\n".join(out) + "\n", encoding="utf-8")
    return header, inventory


def import_library(dll):
    exports = run([TOOLS / "llvm-readobj.exe", "--coff-exports", dll])
    image = dll.read_bytes()
    pe = struct.unpack_from("<I", image, 0x3c)[0]
    section_count = struct.unpack_from("<H", image, pe + 6)[0]
    optional_size = struct.unpack_from("<H", image, pe + 20)[0]
    sections = []
    for i in range(section_count):
        offset = pe + 24 + optional_size + 40 * i
        virtual_size, address, raw_size = struct.unpack_from("<III", image, offset + 8)
        flags = struct.unpack_from("<I", image, offset + 36)[0]
        sections.append((address, address + max(virtual_size, raw_size), flags))
    declarations = []
    for block in re.findall(r"Export \{(.*?)\}", exports, re.S):
        name = re.search(r"Name: (\S+)", block).group(1)
        address = int(re.search(r"RVA: (0x[0-9A-Fa-f]+)", block).group(1), 16)
        executable = any(start <= address < end and flags & 0x20000000
                         for start, end, flags in sections)
        declarations.append(name + ("" if executable else " DATA"))
    if not declarations:
        raise RuntimeError(f"No exports found in {dll}")
    definition = BUILD / "imports" / (dll.stem + ".def")
    definition.write_text("LIBRARY " + dll.name + "\nEXPORTS\n"
                          + "\n".join(declarations) + "\n", encoding="ascii")
    output = definition.with_suffix(".a")
    run([TOOLS / "llvm-dlltool.exe", "-m", "i386:x86-64", "-d", definition,
         "-l", output])
    return output


def build_dynamic_loader(includes, imports):
    # The stock Windows runtime links only its built-in FS providers. Rebuild
    # its unchanged 1.14.5 loader sources with third-party DSO loading enabled.
    config = (SVN / "svn_private_config.hw").read_text(encoding="utf-8")
    config = re.sub(r"^#define SVN_LIBSVN_FS_LINKS_FS_(?:FS|X)\s*$", "",
                    config, flags=re.M)
    config += '\n#define SVN_USE_DSO 1\n#define SVN_DSO_SUFFIX_FMT "%d.dll"\n#define SVN_SOVERSION 0\n'
    (BUILD / "generated/svn_private_config.h").write_text(config, encoding="utf-8")
    args = [TOOLS / "clang.exe", "-std=c17", "-shared", "-O0", "-g",
            "-D_M_X64", "-D_WIN32_WINNT=0x0601", "-Wno-deprecated-declarations",
            "-Wl,--export-all-symbols"]
    for path in includes:
        args.extend(["-I", path])
    args.extend(sorted((SVN / "libsvn_fs").glob("*.c")))
    args.extend(imports)
    args.extend(["-o", RUNTIME / "libsvn_fs-1.dll"])
    run(args)
    expected = set(re.findall(r"^\s*Name: (svn_fs_\S+)$",
                             run([TOOLS / "llvm-readobj.exe", "--coff-exports",
                                  SVN_BIN / "libsvn_fs-1.dll"]), re.M))
    actual = set(re.findall(r"^\s*Name: (svn_fs_\S+)$",
                           run([TOOLS / "llvm-readobj.exe", "--coff-exports",
                                RUNTIME / "libsvn_fs-1.dll"]), re.M))
    if expected - actual:
        raise RuntimeError(f"Rebuilt FS loader is missing exports: {sorted(expected - actual)}")


def build_wc_test_runtime(includes, imports):
    # The distribution omits four WC-private exports needed by entries-dump.
    # Rebuild the unchanged pinned WC sources for the isolated test runtime;
    # preserve all original exports and add the test helper's private exports.
    run([Path(os.sys.executable), SVN.parent / "build/transform_sql.py",
         SVN / "libsvn_wc/wc-queries.sql", BUILD / "generated/wc-queries.h"])
    args = [TOOLS / "clang.exe", "-std=c17", "-shared", "-O0", "-g", "-D_M_X64",
            "-D_WIN32_WINNT=0x0601", "-Wno-deprecated-declarations", "-Wl,--export-all-symbols"]
    for path in includes:
        args.extend(["-I", path])
    args.extend(sorted((SVN / "libsvn_wc").glob("*.c")))
    args.extend(imports)
    args.extend([import_library(SVN_BIN / "libsvn_diff-1.dll"), "-o", RUNTIME / "libsvn_wc-1.dll"])
    run(args)
    expected = set(re.findall(r"^\s*Name: (svn_wc_\S+)$",
                             run([TOOLS / "llvm-readobj.exe", "--coff-exports", SVN_BIN / "libsvn_wc-1.dll"]), re.M))
    actual = set(re.findall(r"^\s*Name: (svn_wc_\S+)$",
                           run([TOOLS / "llvm-readobj.exe", "--coff-exports", RUNTIME / "libsvn_wc-1.dll"]), re.M))
    if expected - actual:
        raise RuntimeError(f"Rebuilt WC library is missing exports: {sorted(expected - actual)}")


def main():
    for path in [BUILD / "generated", BUILD / "imports", RUNTIME,
                 ROOT / "artifacts/build"]:
        path.mkdir(parents=True, exist_ok=True)
    # Use the upstream Windows platform headers without editing vendor sources.
    shutil.copy2(APR / "apr.hw", BUILD / "generated/apr.h")
    shutil.copy2(APU / "apu.hw", BUILD / "generated/apu.h")
    # This Windows distribution exports the character table as an array, while
    # upstream declares a pointer to it. Probe that data ABI before adapting the
    # consumer header; leave vendor files unchanged.
    with os.add_dll_directory(str(SVN_BIN)):
        subr = ctypes.CDLL(str(SVN_BIN / "libsvn_subr-1.dll"))
        table = (ctypes.c_uint32 * 256).in_dll(subr, "svn_ctype_table")
        table_is_array = table[0] == 0x81 and table[ord("A")] == 0xc8 and table[ord("0")] == 0x84
    ctype_header = (SVN / "include/svn_ctype.h").read_text(encoding="utf-8")
    if table_is_array:
        original = "extern const apr_uint32_t *const svn_ctype_table;"
        if original not in ctype_header:
            raise RuntimeError("Pinned character-table declaration changed")
        ctype_header = ctype_header.replace(original, "extern const apr_uint32_t svn_ctype_table[];")
    (BUILD / "generated/svn_ctype.h").write_text(ctype_header, encoding="utf-8")
    header, inventory = generate_stubs()
    imports = [import_library(SVN_BIN / name) for name in
               ["libapr-1.dll", "libaprutil-1.dll", "libsvn_subr-1.dll", "libsvn_delta-1.dll"]]
    includes = [BUILD / "generated", SVN / "include", SVN / "libsvn_fs",
                APR, APU, PG / "include"]
    args = [TOOLS / "clang.exe", "-std=c17", "-shared", "-O0", "-g",
            "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
            "-D_WIN32_WINNT=0x0601"]
    for path in includes:
        args.extend(["-I", path])
    args += [ROOT / "src/libsvn_fs_pg/pgfs.c", *imports, PG / "lib/libpq.lib",
             "-o", RUNTIME / "libsvn_fs_pg-1.dll"]
    run(args)
    for path in SVN_BIN.iterdir():
        if path.suffix.lower() in (".dll", ".exe"):
            shutil.copy2(path, RUNTIME / path.name)
    for path in (PG / "bin").iterdir():
        if path.suffix.lower() == ".dll":
            shutil.copy2(path, RUNTIME / path.name)
    loader_imports = imports + [import_library(SVN_BIN / name) for name in
                               ["libsvn_fs_util-1.dll"]]
    build_dynamic_loader(includes, loader_imports)
    probe_imports = [import_library(RUNTIME / "libsvn_fs-1.dll")]
    probe_args = [TOOLS / "clang.exe", "-std=c17", "-O0", "-g", "-Wall", "-Wextra", "-Werror"]
    for path in includes:
        probe_args.extend(["-I", path])
    probe_args += [ROOT / "tests/fs_probe.c", *imports, *probe_imports,
                   "-o", RUNTIME / "fs_probe.exe"]
    run(probe_args)
    build_wc_test_runtime(includes, imports)
    helper_sources = {name: SVN / ("tests/cmdline/" + name + ".c") for name in
                      ["entries-dump", "lock-helper", "atomic-ra-revprop-change"]}
    helper_sources.update({name: SVN / ("tests/libsvn_wc/" + name + ".c") for name in
                           ["wc-lock-tester", "wc-incomplete-tester"]})
    helper_imports = [import_library(SVN_BIN / name) for name in
                      ["libsvn_client-1.dll", "libsvn_ra-1.dll",
                       "libsvn_repos-1.dll", "libsvn_diff-1.dll"]]
    helper_imports.append(import_library(RUNTIME / "libsvn_wc-1.dll"))
    for helper, helper_source in helper_sources.items():
        helper_args = [TOOLS / "clang.exe", "-std=c17", "-O0", "-g", "-D_M_X64",
                       "-D_WIN32_WINNT=0x0601", "-Wno-deprecated-declarations"]
        for path in includes:
            helper_args.extend(["-I", path])
        helper_args += [helper_source, *imports,
                        *probe_imports, *helper_imports,
                        "-o", RUNTIME / (helper + ".exe")]
        run(helper_args)
    manifest = {"compiler": run([TOOLS / "clang.exe", "--version"]).strip(),
                "svn_version": "1.14.5", "apr_version": "1.7.6",
                "fs_loader_sha256": hashlib.sha256((SVN / "libsvn_fs/fs-loader.h").read_bytes()).hexdigest(),
                "source_sha256": hashlib.sha256((ROOT / "src/libsvn_fs_pg/pgfs.c").read_bytes()).hexdigest(),
                "sources": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((ROOT / "src/libsvn_fs_pg").glob("*"))
                            if p.suffix in (".c", ".inc", ".h")},
                "third_party_fs_loader": True,
                "windows_ctype_array_abi": table_is_array,
                "svn_subr_sha256": hashlib.sha256((SVN_BIN / "libsvn_subr-1.dll").read_bytes()).hexdigest(),
                "dll_sha256": hashlib.sha256((RUNTIME / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest(),
                "callbacks": inventory}
    (ROOT / "artifacts/build/manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dll": str(RUNTIME / "libsvn_fs_pg-1.dll"),
                      "implemented_callbacks": sum(c["status"] == "implemented" for c in inventory),
                      "total_error_callbacks": len(inventory)}))


if __name__ == "__main__":
    main()
