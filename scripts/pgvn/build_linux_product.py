"""Build the complete Linux hosts and Apache modules with pinned private ABIs."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import re
from build_linux import build_component
from build_metadata import collect,write_metadata,sha256,check_runtime
from prepare_upstream import prepare
from prepare_protocol import prepare_protocol
from prepare_dav import prepare_dav
from embed_migrations import embed
from embed_web import embed_web
ROOT=Path(__file__).resolve().parents[2]


def build_product(sdk,httpd,tools,configuration,sanitize=False):
    sdk=Path(sdk).resolve(strict=True); httpd=Path(httpd).resolve(strict=True)
    host_manifest=httpd/"httpd-manifest.json"; host=json.loads(host_manifest.read_text())
    if host["inputs"]["sdk_manifest_sha256"]!=sha256(sdk/"sdk-manifest.json"):
        raise ValueError("Apache host uses another FS SDK")
    if host["inputs"]["lock_sha256"]!=sha256(Path(__file__).with_name("linux-httpd-dependencies.json")):
        raise ValueError("Apache host dependency lock mismatch")
    for relative,expected in host["files"].items():
        path=(httpd/relative).resolve(strict=True)
        if not path.is_relative_to(httpd) or sha256(path)!=expected: raise ValueError("Apache SDK file mismatch: "+relative)
    tools=Path(tools).resolve(strict=True)
    tool_manifest=tools/"tools-manifest.json"; tool_info=json.loads(tool_manifest.read_text())
    if tool_info["inputs"]["sdk_manifest_sha256"]!=sha256(sdk/"sdk-manifest.json"):
        raise ValueError("PostgreSQL tools use another FS SDK")
    for relative,expected in tool_info["files"].items():
        if sha256(tools/relative)!=expected: raise ValueError("PostgreSQL tool mismatch: "+relative)
    base=Path(build_component(sdk,configuration,"fs",sanitize=sanitize)["output"])
    work=ROOT/"build/pgvn/linux"/(configuration.lower()+"-product"+("-sanitized" if sanitize else ""))
    work.mkdir(parents=True,exist_ok=True)
    for directory in ("bin","lib","generated","apache","licenses"):
        target=work/directory
        if target.is_symlink(): raise ValueError("Refusing a symlink product output: "+str(target))
        if target.exists(): shutil.rmtree(target)
    for directory in ("lib","generated"):
        shutil.copytree(base/directory,work/directory,dirs_exist_ok=True,symlinks=True)
    for directory in ("objects","bin","apache/bin","apache/modules","apache/include","licenses"):
        (work/directory).mkdir(parents=True,exist_ok=True)
    metadata=write_metadata(ROOT,work/"generated","linux",configuration)
    env=dict(os.environ); env.pop("LD_PRELOAD",None); env["LD_LIBRARY_PATH"]=str(work/"lib")
    def run(args,timeout=180):
        args=list(map(str,args)); result=subprocess.run(args,cwd=ROOT,env=env,text=True,capture_output=True,timeout=timeout)
        with (work/"commands.jsonl").open("a") as log: log.write(json.dumps({"args":args,"exit_code":result.returncode,"stdout":result.stdout,"stderr":result.stderr})+"\n")
        if result.returncode: raise RuntimeError("Linux product command failed: "+str(args)+"\n"+result.stdout+result.stderr)
        return result.stdout
    svn=sdk/"sources/subversion/subversion"
    repository=work/"upstream/libsvn_repos"; protocol=work/"upstream"; dav=work/"upstream/dav"
    patches={"repositories":prepare(svn/"libsvn_repos",repository,ROOT/"patches/pgvn/subversion-1.14.5/repos.json"),
             "protocol":prepare_protocol(svn,protocol,ROOT/"patches/pgvn/subversion-1.14.5/protocol.json"),
             "dav":prepare_dav(svn,dav,ROOT/"patches/pgvn/subversion-1.14.5/dav.json")}
    embed(ROOT,work/"generated/pgvn_migrations.h"); embed_web(ROOT,work/"generated")
    includes=[work/"generated",ROOT/"src/pgvn/core",ROOT/"src/pgvn/serve",ROOT/"src/pgvn/monitor",ROOT/"src/pgvn/platform",
              ROOT/"src/libsvn_fs_pg",protocol/"svnserve",repository,svn/"include",svn/"libsvn_fs",sdk/"include/subversion-1",
              sdk/"include/apr-1",sdk/"include",svn,sdk/"sources/subversion"]
    flags=["-std=c17","-D_GNU_SOURCE","-D_POSIX_C_SOURCE=200809L","-pthread","-fPIC","-Wall","-Wextra","-Werror","-Wno-unused-parameter"]
    flags += ["-O2","-DNDEBUG"] if configuration=="Release" else ["-O0","-g"]
    if sanitize: flags += ["-fsanitize=address,undefined","-fno-omit-frame-pointer"]
    for directory in includes: flags += ["-I",directory]
    upstream_flags=[f for f in flags if f!="-Werror"]+["-Wno-deprecated-declarations","-Wno-missing-field-initializers"]
    def object_file(source,label,options):
        output=work/"objects"/(label+".o"); run(["cc",*options,"-c",source,"-o",output]); return output
    objects=[]
    core=[p for p in sorted((ROOT/"src/pgvn/core").glob("*.c")) if p.name!="hooks_win.c"]
    core += [ROOT/"src/libsvn_fs_pg/pgfs.c"]
    core += [ROOT/"src/pgvn/platform"/p for p in ("credentials_posix.c","hooks_posix.c","process_posix.c","runtime.c")]
    for source in core: objects.append(object_file(source,"core-"+source.stem,[*flags,"-DPGVN_STATIC"]))
    for source in sorted(repository.glob("*.c")): objects.append(object_file(source,"repos-"+source.stem,upstream_flags))
    archive=work/"lib/libpgvn_core.a"; archive.unlink(missing_ok=True); run(["ar","rcs",archive,*objects])
    libraries=[archive,"-L"+str(work/"lib"),"-Wl,-rpath,$ORIGIN/../lib:$ORIGIN","-lsvn_fs-1","-lsvn_fs_util-1",
               "-lsvn_diff-1","-lsvn_delta-1","-lsvn_subr-1","-laprutil-1","-lapr-1","-lpq","-lcrypto"]
    protocol_objects=[]
    for directory,names in (("svnserve",("serve.c","logger.c","log-escape.c")),
                            ("libsvn_ra_svn",("marshal.c","streams.c","editorp.c","cram.c","deprecated.c"))):
        for name in names: protocol_objects.append(object_file(protocol/directory/name,"protocol-"+Path(name).stem,upstream_flags))
    run(["cc",*flags,ROOT/"src/pgvn/serve/main_posix.c",ROOT/"src/pgvn/serve/listener.c",ROOT/"src/pgvn/serve/protocol.c",
         ROOT/"src/pgvn/monitor/http_posix.c",ROOT/"src/pgvn/monitor/data.c",work/"generated/pgvn_web.c",
         *protocol_objects,*libraries,"-levent","-o",work/"bin/pgvnserve"])
    run(["cc",*flags,*sorted((ROOT/"src/pgvn/admin").glob("*.c")),*libraries,"-o",work/"bin/pgvnadmin"])
    run(["cc",*flags,ROOT/"src/pgvn/platform/process_runner.c","-o",work/"bin/pgvn-hook-runner"])
    shutil.copy2(work/"bin/pgvn-hook-runner",work/"apache/bin/pgvn-hook-runner")
    for name in ("pg_dump","pg_restore","psql","pg_isready"):
        shutil.copy2(tools/"bin"/name,work/"bin"/name)
        run(["patchelf","--set-rpath","$ORIGIN/../lib",work/"bin"/name])
        if metadata["build_identity"]["upstream"]["postgresql"] not in run([work/"bin"/name,"--version"]):
            raise ValueError("PostgreSQL tool runtime version mismatch: "+name)
    # Apache embeds the same APR ABI as the FS SDK. Core server symbols remain
    # resolved by that host; module load validation is mandatory integration work.
    dav_flags=[*upstream_flags,"-I",httpd/"include","-I",dav/"include","-I",dav/"mod_dav_svn","-I",ROOT/"src/pgvn/dav",
               "-include",dav/"pgvn_dav_namespace.h","-DPGVN_REAL_DAV"]
    dav_objects=[]
    for source in sorted((dav/"mod_dav_svn").rglob("*.c")):
        dav_objects.append(object_file(source,"dav-"+source.relative_to(dav/"mod_dav_svn").as_posix().replace('/','-'),dav_flags))
    run(["cc",*dav_flags,"-DPGVN_DAV_BUILD","-shared",ROOT/"src/pgvn/dav/adapter.c",ROOT/"src/pgvn/dav/status.c",ROOT/"src/pgvn/dav/bootstrap.c",
         *dav_objects,*libraries,"-Wl,-rpath,$ORIGIN/../../lib:$ORIGIN","-o",work/"apache/modules/mod_dav_pgvn.so"])
    run(["cc",*dav_flags,"-DPGVN_AUTHZ_BOOTSTRAP","-shared",dav/"mod_authz_svn/mod_authz_svn.c",ROOT/"src/pgvn/dav/bootstrap.c",
         "-L"+str(work/"apache/modules"),"-l:mod_dav_pgvn.so",*libraries,"-Wl,-rpath,$ORIGIN/../../lib:$ORIGIN","-o",work/"apache/modules/mod_authz_pgvn.so"])
    for source in (httpd/"modules").glob("*.so"): shutil.copy2(source,work/"apache/modules"/source.name)
    for name in ("httpd","htpasswd"):
        shutil.copy2(httpd/"bin"/name,work/"apache/bin"/name)
    for name in ("httpd","htpasswd"):
        run(["patchelf","--set-rpath","$ORIGIN/../../lib",work/"apache/bin"/name])
    control=work/"apache/bin/apachectl"
    control.write_text('#!/bin/sh\nset -eu\ncase "${1-}" in start|stop|restart|graceful|graceful-stop) action=$1; shift; set -- -k "$action" "$@";; esac\nexec "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/httpd" "$@"\n')
    control.chmod(0o755)
    for module in (work/"apache/modules").glob("*.so"): run(["patchelf","--set-rpath","$ORIGIN/../../lib:$ORIGIN",module])
    # License texts for the shipped code; system shared libraries remain named
    # distro dependencies and are inventoried in each ELF report.
    for source,paths in ((sdk/"sources/subversion",("LICENSE","NOTICE")),(sdk/"sources/apr",("LICENSE","NOTICE")),
                         (sdk/"sources/apr-util",("LICENSE","NOTICE")),(sdk/"sources/postgresql",("COPYRIGHT",)),(httpd/"source",("LICENSE","NOTICE"))):
        target=work/"licenses"/source.name; target.mkdir(exist_ok=True)
        for name in paths:
            if (source/name).exists(): shutil.copy2(source/name,target/name)
    shutil.copy2(ROOT/"LICENSE",work/"licenses/PgVN-LICENSE"); shutil.copy2(ROOT/"NOTICE",work/"licenses/PgVN-NOTICE")
    apache_compiled_modules=run([work/"apache/bin/httpd","-l"])
    if "event.c" not in apache_compiled_modules.split(): raise ValueError("Pinned Apache host must embed the event MPM")
    runtime={}
    for name in ("pgvnserve","pgvnadmin"):
        runtime[name]=json.loads(run([work/"bin"/name,"--version"])); check_runtime(runtime[name],metadata["build_identity"])
    dependencies={}
    for name in ("bin/pgvnserve","bin/pgvnadmin","bin/pgvn-hook-runner","apache/bin/httpd","apache/modules/mod_dav_pgvn.so","apache/modules/mod_authz_pgvn.so","bin/pg_dump","bin/pg_restore","bin/psql","bin/pg_isready"):
        dependencies[name]=run(["ldd",work/name])
        if "not found" in dependencies[name]: raise ValueError("Unresolved product ELF dependency: "+name)
        if "ELF64" not in run(["readelf","-h",work/name]): raise ValueError("Product is not ELF64: "+name)
        for line in dependencies[name].splitlines():
            match=re.match(r"\s*(lib(?:apr|svn_|pq)\S*) => (\S+) ",line)
            if match and not Path(match[2]).resolve().is_relative_to(work/"lib"): raise ValueError("Unpinned product dependency: "+line)
    if collect(ROOT,"linux",configuration)!=metadata: raise ValueError("Source changed during product build")
    products={"runtime/pgvn/"+p.relative_to(work).as_posix():{"sha256":sha256(p)} for directory in ("bin","lib","apache","licenses") for p in sorted((work/directory).rglob("*")) if p.is_file() and p.suffix!=".a"}
    result={**metadata,"format":1,"scope":"Linux native server, admin, supervised processes and Apache modules; acceptance recorded separately",
            "complete_product":True,"sanitizers":["address","undefined"] if sanitize else [],"sdk_manifest_sha256":sha256(sdk/"sdk-manifest.json"),"httpd_manifest_sha256":sha256(host_manifest),"tools_manifest_sha256":sha256(tool_manifest),"core_archive_sha256":sha256(archive),
            "patches":patches,"runtime_build_info":runtime,"dependencies":dependencies,"products":products,
            "apache_compiled_modules":apache_compiled_modules,"compiler":run(["cc","--version"]).splitlines()[0],"system_packages":run(["dpkg-query","-W","-f=${Package}=${Version}\n"]).splitlines()}
    (work/"build-manifest.json").write_text(json.dumps(result,indent=2)+"\n")
    return {"output":str(work),"manifest":str(work/"build-manifest.json"),"complete_product":True}
