"""Build the pinned Linux Apache host against the verified PgVN FS SDK."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import urllib.request
import shutil
ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name("linux-httpd-dependencies.json")
def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=ROOT/"build/pgvn/linux-httpd-sdk")
    parser.add_argument("--archives",type=Path,default=ROOT/"vendor/linux-archives")
    parser.add_argument("--fetch-only",action="store_true")
    parser.add_argument("--jobs",type=int,default=4)
    args=parser.parse_args()
    lock=json.loads(LOCK.read_text()); sdk=args.sdk.resolve(strict=True)
    sdk_manifest=sdk/"sdk-manifest.json"; manifest=json.loads(sdk_manifest.read_text())
    for name,expected in manifest["files"].items():
        if digest(sdk/name)!=expected: raise ValueError("FS SDK mismatch: "+name)
    archive=args.archives/lock["archive"]
    if not archive.exists() and args.fetch_only:
        archive.parent.mkdir(parents=True,exist_ok=True)
        temp=archive.with_suffix(".download")
        try:
            with urllib.request.urlopen(lock["url"],timeout=90) as response,temp.open("wb") as out: shutil.copyfileobj(response,out)
            if digest(temp)!=lock["sha256"]: raise ValueError("Apache archive checksum mismatch")
            temp.replace(archive)
        finally: temp.unlink(missing_ok=True)
    if digest(archive)!=lock["sha256"]: raise ValueError("Apache archive checksum mismatch")
    if args.fetch_only: print(json.dumps({"archive_verified":str(archive)})); return
    if not 1<=args.jobs<=64: raise ValueError("jobs outside 1..64")
    prefix=args.output.resolve(); inputs={"lock_sha256":digest(LOCK),"script_sha256":digest(Path(__file__)),"sdk_manifest_sha256":digest(sdk_manifest)}
    marker=prefix/".pgvn-httpd-inputs.json"
    if prefix.exists() and any(prefix.iterdir()) and (not marker.exists() or json.loads(marker.read_text())!=inputs):
        raise ValueError("Choose a new output for changed Apache inputs")
    prefix.mkdir(parents=True,exist_ok=True); marker.write_text(json.dumps(inputs,indent=2)+"\n")
    source=prefix/"source"
    if not source.exists():
        extraction=prefix/"extract"; extraction.mkdir()
        with tarfile.open(archive) as package: package.extractall(extraction,filter="data")
        roots=list(extraction.iterdir())
        if len(roots)!=1: raise ValueError("Unexpected Apache archive layout")
        roots[0].rename(source)
    locked=json.loads((ROOT/"src/pgvn/httpd/source-lock.json").read_text())
    for name,item in locked["files"].items():
        if digest(source/name)!=item["sha256"]: raise ValueError("Apache source differs from shared pinned source: "+name)
    environment=dict(os.environ); environment["LD_LIBRARY_PATH"]=str(sdk/"lib")
    environment["LDFLAGS"]="-Wl,-rpath,"+str(sdk/"lib")
    def run(argv):
        print(json.dumps({"args":list(map(str,argv))}),flush=True)
        with (prefix/"commands.log").open("a") as log: subprocess.run(list(map(str,argv)),cwd=source,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=1200,check=True)
    if not (source/"configure").exists(): run(["./buildconf","--with-apr="+str(sdk/"sources/apr")])
    if not (source/"config.status").exists():
        run(["./configure","--prefix="+str(prefix),"--with-apr="+str(sdk/"bin/apr-1-config"),
             "--with-apr-util="+str(sdk/"bin/apu-1-config"),"--with-pcre=/usr","--enable-so","--enable-ssl",
             "--enable-dav","--enable-dav-fs","--enable-auth-basic","--enable-authn-file","--enable-authz-user",
             "--enable-authz-host","--enable-authz-core","--enable-authn-core","--with-mpm=event"])
    run(["make","-j"+str(args.jobs)]); run(["make","install"])
    files={p.relative_to(prefix).as_posix():digest(p) for directory in ("bin","include","modules","build","lib") for p in sorted((prefix/directory).rglob("*")) if p.is_file()}
    result={"format":1,"source":lock,"inputs":inputs,"files":files,"version":subprocess.check_output([prefix/"bin/httpd","-v"],env=environment,text=True),
            "system_packages":subprocess.check_output(["dpkg-query","-W","-f=${Package}=${Version}\n"],text=True).splitlines()}
    (prefix/"httpd-manifest.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({"sdk":str(prefix),"manifest_sha256":digest(prefix/"httpd-manifest.json")}))
if __name__=="__main__": main()
