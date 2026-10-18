"""Native Linux component build against an explicitly verified, pinned SDK.

This stage produces ELF development artifacts, not a deployable PgVN package.
The admin component shares the production listener/core build; monitoring,
Apache/DAV and packaging remain separate.
"""
from pathlib import Path
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile

from build_metadata import collect, sha256, write_metadata, check_runtime
from prepare_upstream import prepare as prepare_upstream
from prepare_protocol import prepare_protocol
from embed_migrations import CATALOG, STORAGE, embed

ROOT = Path(__file__).resolve().parents[2]

def verify_subversion_inputs(svn,work):
    lock=json.loads((ROOT/"scripts/pgvn/linux-dependencies.json").read_text())
    source=next(item for item in lock["sources"] if item["name"]=="subversion")
    archive=ROOT/"vendor/linux-archives"/source["archive"]
    if sha256(archive)!=source["sha256"]:
        raise ValueError("Pinned Subversion archive checksum mismatch")
    specifications=((ROOT/"patches/pgvn/subversion-1.14.5/repos.json","libsvn_repos"),
                    (ROOT/"patches/pgvn/subversion-1.14.5/protocol.json",""))
    names=set()
    for specification,root in specifications:
        spec=json.loads(specification.read_text())
        names.update("subversion-1.14.5/subversion/"+(root+"/" if root else "")+name
                     for name in spec["base_sha256"])
    directories=("libsvn_repos","svnserve","libsvn_ra_svn","libsvn_fs_util","libsvn_diff","libsvn_delta")
    with tarfile.open(archive) as bundle:
        archive_names=set(bundle.getnames())
        for directory in directories:
            source_directory=svn/directory
            if not source_directory.is_dir():
                raise ValueError("Missing consumed Subversion source directory: "+directory)
            for path in sorted(source_directory.rglob("*")):
                if not path.is_file() or path.suffix not in (".c",".h"):
                    continue
                name="subversion-1.14.5/subversion/"+path.relative_to(svn).as_posix()
                if name not in archive_names:
                    raise ValueError("Unpinned Subversion source in consumed directory: "+name)
                names.add(name)
        for name in sorted(names):
            member=bundle.extractfile(name)
            if member is None or sha256_bytes(member.read())!=sha256(svn/name.split("/subversion/",1)[1]):
                raise ValueError("Configured Subversion source differs from pinned archive: "+name)
    return {"archive_sha256":source["sha256"],
            "source_files":{name:sha256(svn/name.split("/subversion/",1)[1]) for name in sorted(names)},
            "generated_private_config_sha256":sha256(svn/"svn_private_config.h")}


def sha256_bytes(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()


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
                svn / "include", svn / "libsvn_fs", svn / "libsvn_subr", sdk / "include/subversion-1",
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
    elif component in ("credentials", "hook-config", "hooks"):
        objects = []
        if component == "credentials":
            sources = ("core/context.c", "core/credentials.c", "platform/credentials_posix.c")
        elif component == "hook-config":
            sources = ("core/context.c", "core/catalog.c", "core/policy.c", "core/policy_protocol.c",
                       "core/hooks_config.c", "core/hooks_common.c")
        else:
            sources = ("core/hooks_config.c", "core/hooks_common.c", "core/hooks_posix.c")
        if component in ("hook-config", "hooks"):
            flags += ["-ffunction-sections", "-fdata-sections"]
        for relative in sources:
            source = ROOT / "src/pgvn" / relative
            output = work / (source.stem + ".o")
            run(["cc", *flags, "-c", source, "-o", output])
            objects.append(output)
        library = work / ("lib/libpgvn_" + component.replace("-", "_") + ".a")
        library.unlink(missing_ok=True)
        run(["ar", "rcs", library, *objects])
        probe_name = {"credentials": "credential_probe", "hook-config": "hook_config_probe",
                      "hooks": "hook_runner_probe"}[component]
        probe = work / "bin" / probe_name
        extras = ["-lcrypto"] if component == "credentials" else ["-lsvn_repos-1", "-Wl,--gc-sections"]
        run(["cc", *flags, ROOT / "tests/pgvn" / (probe_name + ".c"), ROOT / "src/pgvn/core/build_info.c",
             library, *libraries, *extras, "-o", probe])
        elf = probe
    elif component in ("serve", "admin"):
        upstream_inputs=verify_subversion_inputs(svn,work)
        flags += ["-I",svn,"-I",ROOT/"src/pgvn/serve"]
        repository_source=work/"upstream/libsvn_repos"
        repository_hashes=prepare_upstream(svn/"libsvn_repos",repository_source,
            ROOT/"patches/pgvn/subversion-1.14.5/repos.json")
        protocol_hashes=prepare_protocol(svn,work/"upstream",
            ROOT/"patches/pgvn/subversion-1.14.5/protocol.json")
        legacy.BUILD,legacy.SVN,legacy.ROOT=work,svn,ROOT
        callbacks=legacy.generate_stubs()[1]
        migration_inputs=None
        if component=="admin":
            embed(ROOT,work/"generated/pgvn_migrations.h")
            migration_inputs={
                "catalog":{name:sha256(ROOT/"sql/pgvn"/name) for name in CATALOG},
                "storage":{name:sha256(ROOT/"sql"/name) for name in STORAGE},
                "embedded_header_sha256":sha256(work/"generated/pgvn_migrations.h")}
        for path in (work/"upstream/svnserve",work/"upstream/libsvn_ra_svn",repository_source,
                     svn/"svnserve",svn/"libsvn_repos",svn/"libsvn_ra_svn",svn/"libsvn_delta",
                     svn/"libsvn_diff",svn/"libsvn_fs_util"):
            flags += ["-I",path]
        sources=[]
        for source in sorted((ROOT/"src/pgvn/core").glob("*.c")):
            if source.name=="hooks_win.c":
                continue
            sources.append(("core-"+source.stem,source,flags))
        sources.append(("core-credentials-posix",ROOT/"src/pgvn/platform/credentials_posix.c",flags))
        sources.append(("core-pgfs",ROOT/"src/libsvn_fs_pg/pgfs.c",flags))
        upstream_flags=[flag for flag in flags if flag!="-Werror"]
        upstream_flags += ["-Wno-deprecated-declarations","-Wno-missing-field-initializers"]
        for source in sorted(repository_source.glob("*.c")):
            sources.append(("repos-"+source.stem,source,upstream_flags))
        objects=[]
        for name,source,compile_flags in sources:
            output=work/(name+".o")
            run(["cc",*compile_flags,"-DPGVN_STATIC","-c",source,"-o",output])
            objects.append(output)
        library=work/"lib/libpgvn_serve_core.a"
        library.unlink(missing_ok=True)
        run(["ar","rcs",library,*objects])
        protocol_sources=[work/"upstream/svnserve"/name for name in ("serve.c","logger.c","log-escape.c")]
        protocol_sources += [work/"upstream/libsvn_ra_svn"/name for name in
                             ("marshal.c","streams.c","editorp.c","cram.c","deprecated.c")]
        protocol_objects=[]
        for source in protocol_sources:
            output=work/("protocol-"+source.stem+".o")
            run(["cc",*upstream_flags,"-c",source,"-o",output])
            protocol_objects.append(output)
        probe=work/"bin/pgvnserve"
        server_sources=[ROOT/"src/pgvn/serve/listener.c",ROOT/"src/pgvn/serve/listener_posix.c",
                        ROOT/"src/pgvn/serve/main.c",ROOT/"src/pgvn/serve/main_posix.c",
                        ROOT/"src/pgvn/serve/protocol.c",ROOT/"src/pgvn/core/build_info.c"]
        run(["cc",*flags,"-DPGVN_STATIC",*server_sources,*protocol_objects,library,
             *libraries,"-lsvn_fs_util-1","-lsvn_diff-1","-lcrypto","-pthread",
             "-Wl,-z,defs","-o",probe])
        products=[{"name":"pgvnserve","path":"bin/pgvnserve","role":"supporting_service"}]
        if component=="serve":
            fixture=work/"bin/serve_fixture"
            run(["cc",*flags,"-DPGVN_STATIC",ROOT/"tests/pgvn/serve_fixture.c",library,*libraries,
                 "-lsvn_fs_util-1","-lsvn_diff-1","-lcrypto","-pthread","-Wl,-z,defs","-o",fixture])
            products.append({"name":"serve_fixture","path":"bin/serve_fixture","role":"test_fixture"})
        else:
            admin=work/"bin/pgvnadmin"
            admin_sources=sorted((ROOT/"src/pgvn/admin").glob("*.c"))
            run(["cc",*flags,"-DPGVN_STATIC",*admin_sources,library,*libraries,
                 "-lsvn_fs_util-1","-lsvn_diff-1","-lcrypto","-pthread",
                 "-Wl,-z,defs","-o",admin])
            products.insert(0,{"name":"pgvnadmin","path":"bin/pgvnadmin","role":"administrative_cli"})
        upstream_inputs.update({"repository_patch_sha256":sha256(ROOT/"patches/pgvn/subversion-1.14.5/repos.json"),
            "protocol_patch_sha256":sha256(ROOT/"patches/pgvn/subversion-1.14.5/protocol.json"),
            "repository_source_sha256":repository_hashes,"protocol_source_sha256":protocol_hashes,
            "applied_repositories_patch_sha256":sha256(work/"upstream/applied-repos.patch"),
            "applied_protocol_patch_sha256":sha256(work/"upstream/applied-protocol.patch"),
            "pgfs_stub_inventory_sha256":sha256(work/"generated/pgfs_stubs.h")})
        elf=probe
    else:
        raise ValueError("Unknown Linux component")
    if component=="admin":
        admin_runtime=json.loads(run([work/"bin/pgvnadmin","--version"]))
        check_runtime(admin_runtime,metadata["build_identity"])
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
    scope=("Native Linux foreground SVN listener and production protocol/core; administration, monitoring, Apache/DAV and packages not built"
           if component=="serve" else
           "Native Linux administrative CLI and supporting foreground SVN listener using production protocol/core; monitoring, Apache/DAV and packages not built"
           if component=="admin" else
           "Linux "+component+" development artifacts; remaining hosts/package not built")
    result = {"format": 1, "scope": scope,
              "build_identity": metadata["build_identity"], "sdk_manifest_sha256": sha256(manifest_path),
              "fs_loader_header_sha256": sha256(svn / "libsvn_fs/fs-loader.h") if component == "fs" else None,
              "component": component, "sanitizers": ["address", "undefined"] if sanitize else [],
              "content_transfer": content_transfer if component == "fs" else None,
              "runtime": runtime, "dependencies": paths,
              "system_dependencies": {name: {"path": path, "sha256": sha256(Path(path))}
                                      for name, path in paths.items() if not Path(path).resolve().is_relative_to(work)},
              "compiler": run(["cc", "--version"]).splitlines()[0],
              "sdk_dependencies": copied,
              "elf_dynamic": run(["readelf", "-d", elf]), "callbacks": callbacks,
              "products":products if component in ("serve","admin") else [],
              "admin_runtime":admin_runtime if component=="admin" else None,
              "admin_migrations":migration_inputs if component=="admin" else None,
              "upstream_inputs":upstream_inputs if component in ("serve","admin") else None,
              "files": {p.relative_to(work).as_posix(): sha256(p) for directory in ("bin", "lib")
                        for p in sorted((work / directory).iterdir()) if p.is_file()}}
    (work / "build-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return {"output": str(work), "manifest": str(work / "build-manifest.json"),
            "components_built": [component], "complete_product": False}
