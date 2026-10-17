"""Build the pinned FS SDK in a user-owned prefix; never install system libraries.

Run in linux-builder.Dockerfile's Ubuntu 24.04 toolchain. Fetching is opt-in;
normal builds use verified local archives and need no network.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name("linux-dependencies.json")


def digest(path):
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def inventory(prefix):
    return {p.relative_to(prefix).as_posix(): digest(p)
            for directory in ("bin", "lib", "include", "sources/subversion/subversion/include",
                              "sources/subversion/subversion/libsvn_fs")
            for p in sorted((prefix / directory).rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, default=ROOT / "vendor/linux-archives")
    parser.add_argument("--output", type=Path, default=ROOT / "build/pgvn/linux-sdk")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--fetch-only", action="store_true")
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text())
    descriptor = json.loads((ROOT / "src/pgvn/version.json").read_text())["upstream"]
    for source in lock["sources"]:
        key = "apr_util" if source["name"] == "apr-util" else source["name"]
        if descriptor[key] != source["version"]:
            raise ValueError("SDK lock disagrees with product descriptor: " + key)
        archive = args.archives / source["archive"]
        if not archive.exists() and args.fetch_only:
            archive.parent.mkdir(parents=True, exist_ok=True)
            temporary = archive.with_suffix(".download")
            try:
                with urllib.request.urlopen(source["url"], timeout=90) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                if digest(temporary) != source["sha256"]:
                    raise ValueError("Download checksum mismatch: " + source["name"])
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
        if not archive.is_file() or digest(archive) != source["sha256"]:
            raise ValueError("Missing or mismatched archive: " + str(archive))
    if args.fetch_only:
        print(json.dumps({"archives_verified": len(lock["sources"])}))
        return
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("The first Linux SDK supports x86_64 Linux only")
    if not 1 <= args.jobs <= 64:
        raise ValueError("--jobs must be between 1 and 64")
    prefix = args.output.resolve()
    marker = prefix / ".pgvn-sdk-inputs.json"
    inputs = {"lock_sha256": digest(LOCK), "script_sha256": digest(Path(__file__))}
    if prefix.exists() and any(prefix.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text()) != inputs:
            raise ValueError("SDK output is nonempty or belongs to different inputs; choose a new --output")
    prefix.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(inputs, indent=2) + "\n")
    env = dict(os.environ)
    env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
    # Absolute RUNPATHs are confined to this build-only SDK. Product outputs use
    # $ORIGIN and a separately inventoried dependency directory.
    env["LDFLAGS"] = "-Wl,-rpath," + str(prefix / "lib")
    env["PKG_CONFIG_PATH"] = str(prefix / "lib/pkgconfig")
    env["LD_LIBRARY_PATH"] = str(prefix / "lib")

    def run(argv, cwd):
        argv = [str(a) for a in argv]
        print(json.dumps({"command": argv, "cwd": str(cwd)}), flush=True)
        with (prefix / "commands.log").open("a") as output:
            output.write(json.dumps({"args": argv, "cwd": str(cwd)}) + "\n")
            output.flush()
            subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                           check=True, timeout=1200)

    for source in lock["sources"]:
        work = prefix / "sources" / source["name"]
        if not work.exists():
            extraction = prefix / "extract" / source["name"]
            extraction.mkdir(parents=True)
            with tarfile.open(args.archives.resolve() / source["archive"]) as archive:
                archive.extractall(extraction, filter="data")
            roots = list(extraction.iterdir())
            if len(roots) != 1 or not roots[0].is_dir():
                raise ValueError("Unexpected archive layout")
            work.parent.mkdir(exist_ok=True)
            roots[0].rename(work)
        if source["name"] == "apr":
            if not (work / "configure").exists():
                run(["./buildconf"], work)
            options = ["--enable-threads", "--disable-static"]
        elif source["name"] == "apr-util":
            if not (work / "configure").exists():
                run(["./buildconf", "--with-apr=" + str(prefix / "sources/apr")], work)
            options = ["--with-apr=" + str(prefix / "bin/apr-1-config"), "--with-expat=/usr", "--disable-static"]
        elif source["name"] == "subversion":
            if not (work / "configure").exists():
                run(["./autogen.sh"], work)
            options = ["--with-apr=" + str(prefix / "bin/apr-1-config"),
                       "--with-apr-util=" + str(prefix / "bin/apu-1-config"),
                       "--without-apxs", "--without-serf", "--without-berkeley-db",
                       "--without-swig", "--disable-nls", "--disable-static"]
        else:
            options = ["--without-icu", "--without-readline", "--with-ssl=openssl"]
        if not (work / "config.status").exists():
            run(["./configure", "--prefix=" + str(prefix), *options], work)
        if source["name"] == "postgresql":
            run(["make", "-j" + str(args.jobs), "-C", "src/interfaces/libpq"], work)
            run(["make", "-C", "src/interfaces/libpq", "install"], work)
            run(["make", "-C", "src/bin/pg_config", "install"], work)
            run(["make", "-C", "src/include", "install"], work)
        else:
            run(["make", "-j" + str(args.jobs)], work)
            run(["make", "install"], work)
    manifest = {"format": 1, "scope": lock["scope"], "inputs": inputs,
                "sources": lock["sources"], "prefix": str(prefix),
                "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
                "system_packages": subprocess.check_output(["dpkg-query", "-W", "-f=${Package}=${Version}\n"], text=True).splitlines(),
                "files": inventory(prefix)}
    (prefix / "sdk-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"sdk": str(prefix), "manifest_sha256": digest(prefix / "sdk-manifest.json")}))


if __name__ == "__main__":
    main()
