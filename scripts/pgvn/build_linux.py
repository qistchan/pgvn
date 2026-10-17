"""Native Linux FS build against an explicitly verified, pinned SDK.

This stage produces ELF development artifacts, not a deployable PgVN package.
The remaining hosts (serve/admin/Apache/monitor) are separate L1 work.
"""
from pathlib import Path
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess

from build_metadata import collect, sha256, write_metadata, check_runtime

ROOT = Path(__file__).resolve().parents[2]


def build_component(sdk, configuration, component="fs", content_transfer="binary", sanitize=False):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Native component builds require x86_64 Linux")
    sdk = Path(sdk).resolve(strict=True)
    manifest_path = sdk / "sdk-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    lock_path = Path(__file__).with_name("linux-dependencies.json")
    if manifest["inputs"]["lock_sha256"] != sha256(lock_path):
        raise ValueError("SDK dependency lock mismatch")
    if manifest["sources"] != json.loads(lock_path.read_text())["sources"]:
        raise ValueError("SDK source provenance mismatch")
    for name, expected in manifest["files"].items():
        path = (sdk / name).resolve(strict=True)
        if not path.is_relative_to(sdk) or sha256(path) != expected:
            raise ValueError("SDK file mismatch: " + name)
    suffix = content_transfer if component == "fs" else component
    if sanitize:
        suffix += "-sanitized"
    work = ROOT / "build/pgvn/linux" / (configuration.lower() + "-" + suffix)
    for directory in ("generated", "bin", "lib"):
        (work / directory).mkdir(parents=True, exist_ok=True)
    metadata = write_metadata(ROOT, work / "generated", "linux", configuration)
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env["LD_LIBRARY_PATH"] = str(work / "lib") + os.pathsep + str(sdk / "lib")

    def run(args):
        args = [str(a) for a in args]
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=180)
        with (work / "commands.jsonl").open("a") as log:
            log.write(json.dumps({"args": args, "exit_code": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if result.returncode:
            raise RuntimeError("Linux build command failed: " + str(args) + "\n" + result.stdout + result.stderr)
        return result.stdout

    copied = {}
    for source in sorted((sdk / "lib").glob("*.so*")):
        if not source.is_file():
            continue
        destination = work / "lib" / source.name
        if destination.is_symlink():
            destination.unlink()
        if source.is_symlink():
            target = os.readlink(source)
            if Path(target).is_absolute() or "/" in target:
                raise ValueError("Unexpected SDK library link")
            destination.symlink_to(target)
        else:
            shutil.copy2(source, destination)
            run(["patchelf", "--set-rpath", "$ORIGIN", destination])
        copied[source.name] = {"sdk_sha256": sha256(source)}
    for name in copied:
        copied[name]["runtime_sha256"] = sha256(work / "lib" / name)
    for name in ("svn", "svnadmin", "svnlook"):
        destination = work / "bin" / name
        shutil.copy2(sdk / "bin" / name, destination)
        run(["patchelf", "--set-rpath", "$ORIGIN/../lib", destination])

    svn = sdk / "sources/subversion/subversion"
    legacy_spec = importlib.util.spec_from_file_location("pgfs_build_helpers", ROOT / "scripts/build.py")
    legacy = importlib.util.module_from_spec(legacy_spec)
    legacy_spec.loader.exec_module(legacy)
    legacy.BUILD, legacy.SVN, legacy.ROOT = work, svn, ROOT
    callbacks = legacy.generate_stubs()[1] if component == "fs" else []
    includes = [work / "generated", ROOT / "src/libsvn_fs_pg", ROOT / "src/pgvn/core",
                svn / "include", svn / "libsvn_fs", sdk / "include/subversion-1",
                sdk / "include/apr-1", sdk / "include"]
    flags = ["-std=c17", "-D_POSIX_C_SOURCE=200809L", "-pthread", "-fPIC",
             "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    flags += ["-O2", "-DNDEBUG"] if configuration == "Release" else ["-O0", "-g"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    flags += ["-DPGFS_CONTENT_BINARY=" + ("1" if content_transfer == "binary" else "0")]
    for path in includes:
        flags += ["-I", path]
    libraries = ["-L" + str(work / "lib"), "-L" + str(sdk / "lib"),
                 "-Wl,-rpath,$ORIGIN/../lib:$ORIGIN",
                 "-lsvn_fs-1", "-lsvn_delta-1", "-lsvn_subr-1", "-laprutil-1", "-lapr-1", "-lpq"]
    if component == "fs":
        library = work / "lib/libsvn_fs_pg-1.so.0"
        run(["cc", *flags, "-shared", "-Wl,-z,defs", "-Wl,-soname,libsvn_fs_pg-1.so.0",
             ROOT / "src/libsvn_fs_pg/pgfs.c", *libraries, "-o", library])
        link = work / "lib/libsvn_fs_pg-1.so"
        if link.is_symlink():
            link.unlink()
        if link.exists():
            raise ValueError("Refusing to replace a non-symlink library alias")
        link.symlink_to(library.name)
        probe = work / "bin/fs_linux_probe"
        run(["cc", *flags, ROOT / "tests/pgvn/fs_linux_probe.c", ROOT / "src/pgvn/core/build_info.c",
             "-Wl,--export-dynamic", "-ldl", "-lsvn_fs_pg-1", *libraries, "-o", probe])
        elf = library
    elif component in ("credentials", "hook-config"):
        objects = []
        sources = ("core/context.c", "core/credentials.c", "platform/credentials_posix.c") if component == "credentials" else (
            "core/context.c", "core/catalog.c", "core/policy.c", "core/policy_protocol.c", "core/hooks_config.c")
        if component == "hook-config":
            flags += ["-ffunction-sections", "-fdata-sections"]
        for relative in sources:
            source = ROOT / "src/pgvn" / relative
            output = work / (source.stem + ".o")
            run(["cc", *flags, "-c", source, "-o", output])
            objects.append(output)
        library = work / ("lib/libpgvn_" + component.replace("-", "_") + ".a")
        library.unlink(missing_ok=True)
        run(["ar", "rcs", library, *objects])
        probe_name = "credential_probe" if component == "credentials" else "hook_config_probe"
        probe = work / "bin" / probe_name
        extras = ["-lcrypto"] if component == "credentials" else ["-lsvn_repos-1", "-Wl,--gc-sections"]
        run(["cc", *flags, ROOT / "tests/pgvn" / (probe_name + ".c"), ROOT / "src/pgvn/core/build_info.c",
             library, *libraries, *extras, "-o", probe])
        elf = probe
    else:
        raise ValueError("Unknown Linux component")
    runtime = json.loads(run([probe, "--version"]))
    check_runtime(runtime, metadata["build_identity"])
    if run([work / "bin/svn", "--version", "--quiet"]).strip() != metadata["build_identity"]["svn_base"]:
        raise ValueError("Runtime Subversion version mismatch")
    dependencies = run(["ldd", elf])
    if "not found" in dependencies:
        raise ValueError("Unresolved ELF dependency")
    principal = ("libapr-1", "libaprutil-1", "libsvn_", "libpq")
    paths = {}
    for line in dependencies.splitlines():
        match = re.match(r"\s*(\S+) => (\S+) ", line)
        if match:
            name, path = match.groups()
            paths[name] = path
            if name.startswith(principal) and not Path(path).resolve().is_relative_to(work / "lib"):
                raise ValueError("Unpinned principal ELF dependency: " + line)
    if component == "fs":
        exports = run(["nm", "-D", "--defined-only", library])
        if not re.search(r"\bT svn_fs_pg__init$", exports, re.M):
            raise ValueError("Missing FS loader entry point")
    header = run(["readelf", "-h", elf])
    if "ELF64" not in header or "X86-64" not in header:
        raise ValueError("Unexpected ELF architecture")
    if collect(ROOT, "linux", configuration) != metadata:
        raise ValueError("Source changed during Linux build")
    result = {"format": 1, "scope": "Linux " + component + " development artifacts; hosts/package not built",
              "build_identity": metadata["build_identity"], "sdk_manifest_sha256": sha256(manifest_path),
              "fs_loader_header_sha256": sha256(svn / "libsvn_fs/fs-loader.h"),
              "component": component, "sanitizers": ["address", "undefined"] if sanitize else [],
              "content_transfer": content_transfer if component == "fs" else None,
              "runtime": runtime, "dependencies": paths,
              "system_dependencies": {name: {"path": path, "sha256": sha256(Path(path))}
                                      for name, path in paths.items() if not Path(path).resolve().is_relative_to(work)},
              "compiler": run(["cc", "--version"]).splitlines()[0],
              "sdk_dependencies": copied,
              "elf_dynamic": run(["readelf", "-d", elf]), "callbacks": callbacks,
              "files": {p.relative_to(work).as_posix(): sha256(p) for directory in ("bin", "lib")
                        for p in sorted((work / directory).iterdir()) if p.is_file()}}
    (work / "build-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return {"output": str(work), "manifest": str(work / "build-manifest.json"),
            "components_built": [component], "complete_product": False}
