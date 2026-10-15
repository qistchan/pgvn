"""Build unchanged public FS/repos semantic tests against the pinned runtime."""
from pathlib import Path
import hashlib
import json
import build

ROOT = build.ROOT
SVN = build.SVN
BIN = build.RUNTIME
includes = [build.BUILD / "generated", SVN / "include", SVN / "libsvn_fs",
            build.APR, build.APU, build.PG / "include"]
imports = [build.import_library(BIN / name) for name in
           ["libapr-1.dll", "libaprutil-1.dll", "libsvn_fs-1.dll", "libsvn_delta-1.dll",
            "libsvn_subr-1.dll", "libsvn_repos-1.dll", "libsvn_fs_util-1.dll"]]
support = [SVN / "tests/svn_test_main.c", SVN / "tests/svn_test_fs.c"]
manifest = {}
for name, source in [("fs-test", SVN / "tests/libsvn_fs/fs-test.c"),
                     ("repos-test", SVN / "tests/libsvn_repos/repos-test.c")]:
    args = [build.TOOLS / "clang.exe", "-std=c17", "-O0", "-g", "-D_M_X64",
            "-D_WIN32_WINNT=0x0601", "-Wno-deprecated-declarations"]
    for include in includes:
        args.extend(["-I", include])
    extra = [SVN / "tests/libsvn_repos/dir-delta-editor.c"] if name == "repos-test" else []
    args += [source, *extra, *support, *imports, "-o", BIN / (name + ".exe")]
    build.run(args)
    manifest[name] = {"binary_sha256": hashlib.sha256((BIN / (name + ".exe")).read_bytes()).hexdigest(),
                      "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
(ROOT / "artifacts/build/native-test-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
print(json.dumps({"native_tests": list(manifest)}))
