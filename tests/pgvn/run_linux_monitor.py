"""Build and exercise the Linux monitoring data backend in isolated PostgreSQL."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts/pgvn"))
from build_metadata import collect,sha256
BUILDER = "pgvn-linux-builder:24.04"
POSTGRES = "postgres:18.6"

class FramedProbe:
    def __init__(self,command,environment=None):
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,bufsize=0,env=environment)
        self.responses=queue.Queue()
        self.stderr_chunks=[]
        self.history=[]
        self.stderr_thread=threading.Thread(target=self._drain_stderr,daemon=True)
        self.stdout_thread=threading.Thread(target=self._read_responses,daemon=True)
        self.stderr_thread.start()
        self.stdout_thread.start()

    def _drain_stderr(self):
        while True:
            chunk=self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_chunks.append(chunk)

    def _read_responses(self):
        stream=self.process.stdout
        while True:
            header=stream.readline()
            if not header:
                if self.process.poll() is None:
                    self.responses.put(RuntimeError("monitor probe closed stdout without exiting"))
                else:
                    self.responses.put(RuntimeError(
                        "monitor probe exited before response: "+self.stderr_text()[-2000:]))
                return
            if not header.startswith(b"PGVN_RESPONSE "):
                self.responses.put(RuntimeError("invalid monitor probe response frame"))
                return
            try:
                length=int(header.split()[1])
            except (IndexError,ValueError):
                self.responses.put(RuntimeError("invalid monitor probe response length"))
                return
            chunks=[]
            remaining=length
            while remaining:
                chunk=stream.read(min(remaining,65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining-=len(chunk)
            payload=b"".join(chunks)
            if len(payload)!=length or stream.read(1)!=b"\n":
                self.responses.put(RuntimeError("truncated monitor probe response frame"))
                return
            self.responses.put(payload)

    def stderr_text(self):
        return b"".join(self.stderr_chunks).decode("utf-8","replace")

    def request(self,command,timeout=10,raw=None):
        wire=raw if raw is not None else command.encode("utf-8")
        try:
            self.process.stdin.write(wire+b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError,OSError):
            raise RuntimeError("monitor probe stdin closed: "+self.stderr_text()[-2000:]) from None
        try:
            response=self.responses.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("timed out waiting for monitor probe response: "+command) from None
        if isinstance(response,Exception):
            raise response
        try:
            text=response.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("monitor probe returned non-UTF-8 response") from None
        self.history.append((command,text))
        return text

    def wait(self,timeout=10):
        try:
            returncode=self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            raise
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        return returncode

    def terminate(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)

    def formatted_history(self,redact):
        return "\n".join(redact(command)+" => "+redact(response)
                         for command,response in self.history)+"\n"

def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", required=True)
    parser.add_argument("--configuration", choices=("Release", "Debug"), default="Release")
    parser.add_argument("--sanitize", action="store_true")
    parser.add_argument("--builder-image", default=BUILDER)
    parser.add_argument("--postgres-image", default=POSTGRES)
    args = parser.parse_args()
    if args.postgres_image != POSTGRES:
        raise SystemExit("monitor acceptance requires postgres:18.6")
    sdk_path=Path(args.sdk).expanduser()
    if not sdk_path.is_absolute():
        sdk_path=ROOT/sdk_path
    sdk_path=sdk_path.resolve()
    try:
        sdk_relative=sdk_path.relative_to(ROOT)
    except ValueError:
        raise SystemExit("--sdk must be inside the mounted repository") from None
    if not (sdk_path/"sdk-manifest.json").is_file():
        raise SystemExit("SDK manifest is missing: "+str(sdk_path/"sdk-manifest.json"))
    args.sdk=sdk_relative.as_posix()
    docker=["docker","--host=unix:///var/run/docker.sock"]
    environment=dict(os.environ)
    for name in ("DOCKER_HOST","DOCKER_CONTEXT","DOCKER_TLS","DOCKER_TLS_VERIFY","DOCKER_CERT_PATH"):
        environment.pop(name,None)
    suffix = "sanitized" if args.sanitize else "plain"
    artifact = ROOT / "artifacts/pgvn/linux-monitor" / ("linux-monitor-" + uuid.uuid4().hex[:12])
    artifact.mkdir(parents=True)
    host_os=Path("/etc/os-release").read_text(encoding="utf-8") if Path("/etc/os-release").exists() else platform.platform()
    secrets_seen = []
    sanitizer_outputs = []
    db = "pgvn-monitor-db-" + uuid.uuid4().hex[:8]
    checks = []
    owned_containers = []
    build_logs = []
    frozen_components = []
    frozen_metadata = None
    frozen_manifest = None
    success_report = None
    cleanup_errors = []
    def component_dir(component):
        return args.configuration.lower()+"-"+component+("-sanitized" if args.sanitize else "")
    def redact(value):
        for secret in secrets_seen:
            value=value.replace(secret,"<redacted>")
        return value
    def check(name, condition):
        if not condition:
            raise RuntimeError("failed check: " + name)
        checks.append(name)
    def docker_command(arguments, network=False, input_text=None, timeout=120):
        name="pgvn-monitor-test-"+uuid.uuid4().hex[:8]
        owned_containers.append(name)
        command = docker + ["run", "--rm", "--name", name]
        component=("admin" if "--components" in arguments and
                   arguments[arguments.index("--components")+1]=="admin" else
                   "admin" if arguments and str(arguments[0]).endswith("/pgvnadmin") else
                   "monitor")
        if network:
            command += ["--network", "container:" + db]
        else:
            command += ["--network","none"]
        command += ["--user",str(os.getuid())+":"+str(os.getgid()),
                    "--mount", f"type=bind,src={ROOT},dst=/repo", "-w", "/repo",
                    "-e", "HOME=/tmp", "-e",
                    "LD_LIBRARY_PATH=/repo/build/pgvn/linux/" +
                    component_dir(component) + "/lib:/repo/" +
                    args.sdk + "/lib", "--tmpfs", "/tmp:rw,size=64m", args.builder_image]
        if args.sanitize:
            command[-1:-1]=["-e","ASAN_OPTIONS=abort_on_error=1:detect_leaks=1",
                            "-e","UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1"]
        command += arguments
        result=subprocess.run(command, input=input_text, text=True,env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        sanitizer_outputs.extend((result.stdout,result.stderr))
        if arguments[:3]==["python3","scripts/pgvn/build.py","--target"]:
            build_logs.append({"component":arguments[arguments.index("--components")+1],
                               "exit_code":result.returncode,
                               "stdout":result.stdout,"stderr":result.stderr})
            if result.returncode:
                raise RuntimeError("Linux "+component+" build failed; see frozen build logs")
        return result

    def freeze_component(component):
        nonlocal frozen_metadata,frozen_manifest
        work=ROOT/"build/pgvn/linux"/(args.configuration.lower()+"-"+component+
                                     ("-sanitized" if args.sanitize else ""))
        manifest=json.loads((work/"build-manifest.json").read_text(encoding="utf-8"))
        inputs=json.loads((work/"generated/build-inputs.json").read_text(encoding="utf-8"))
        current=collect(ROOT,"linux",args.configuration)
        if current!=inputs or manifest["build_identity"]!=inputs["build_identity"]:
            raise RuntimeError(component+" build metadata differs from current source")
        if manifest.get("complete_product") is not False:
            raise RuntimeError(component+" manifest must retain complete_product=false")
        for product in manifest["products"]:
            if product["path"] not in manifest["files"]:
                raise RuntimeError(component+" manifest omits a product hash: "+product["path"])
        sdk_manifest=ROOT/args.sdk/"sdk-manifest.json"
        sdk_hash=sha256(sdk_manifest)
        if manifest["sdk_manifest_sha256"]!=sdk_hash:
            raise RuntimeError(component+" SDK manifest hash mismatch")
        frozen=artifact/"inputs"/component
        (frozen/"source").mkdir(parents=True,exist_ok=True)
        shutil.copy2(work/"build-manifest.json",frozen/"build-manifest.json")
        shutil.copy2(work/"generated/build-inputs.json",frozen/"build-inputs.json")
        shutil.copy2(sdk_manifest,frozen/"sdk-manifest.json")
        (frozen/"generated").mkdir(parents=True,exist_ok=True)
        generated_hashes={}
        for generated in sorted((work/"generated").iterdir()):
            if not generated.is_file():
                continue
            digest=sha256(generated)
            generated_hashes[generated.name]=digest
            shutil.copy2(generated,frozen/"generated"/generated.name)
            if sha256(frozen/"generated"/generated.name)!=digest:
                raise RuntimeError("generated file copy hash mismatch: "+generated.name)
        if component=="monitor":
            monitor_inputs=manifest["monitor_inputs"]
            for relative,digest in monitor_inputs["sources"].items():
                if inputs["sources"].get("src/pgvn/"+relative)!=digest:
                    raise RuntimeError("monitor source provenance mismatch: "+relative)
            for key,path in (("probe_sha256","tests/pgvn/linux_monitor_probe.c"),
                             ("build_info_sha256","src/pgvn/core/build_info.c")):
                if inputs["sources"].get(path)!=monitor_inputs[key]:
                    raise RuntimeError("monitor input provenance mismatch: "+path)
            for name,digest in monitor_inputs["assets"].items():
                if inputs["sources"].get("web/pgvn-monitor/"+name)!=digest:
                    raise RuntimeError("monitor asset provenance mismatch: "+name)
            for relative,digest in monitor_inputs["migrations"].items():
                if inputs["sources"].get(relative)!=digest:
                    raise RuntimeError("monitor migration provenance mismatch: "+relative)
            if generated_hashes.get("pgvn_web.h")!=monitor_inputs["web_header_sha256"]:
                raise RuntimeError("embedded web header provenance mismatch")
        (frozen/"generated-hashes.json").write_text(
            json.dumps(generated_hashes,indent=2)+"\n",encoding="utf-8")
        for relative,digest in inputs["sources"].items():
            source=ROOT/relative
            if sha256(source)!=digest:
                raise RuntimeError("source changed before freeze: "+relative)
            target=frozen/"source"/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if sha256(target)!=digest:
                raise RuntimeError("source snapshot hash mismatch: "+relative)
        for relative,digest in manifest["files"].items():
            source=work/relative
            if sha256(source)!=digest:
                raise RuntimeError("product hash differs from manifest: "+relative)
            target=frozen/"products"/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if sha256(target)!=digest:
                raise RuntimeError("product copy hash mismatch: "+relative)
        if sha256(frozen/"sdk-manifest.json")!=sdk_hash:
            raise RuntimeError(component+" SDK manifest copy hash mismatch")
        if component=="admin":
            upstream=manifest.get("upstream_inputs")
            if upstream:
                lock=json.loads((ROOT/"scripts/pgvn/linux-dependencies.json").read_text())
                source=next(item for item in lock["sources"] if item["name"]=="subversion")
                archive=ROOT/"vendor/linux-archives"/source["archive"]
                archive_hash=sha256(archive)
                if archive_hash!=upstream["archive_sha256"]:
                    raise RuntimeError("Subversion source archive hash mismatch")
                (frozen/"upstream").mkdir(parents=True,exist_ok=True)
                shutil.copy2(archive,frozen/"upstream"/archive.name)
                if sha256(frozen/"upstream"/archive.name)!=archive_hash:
                    raise RuntimeError("Subversion source archive snapshot mismatch")
                upstream_source=ROOT/args.sdk/"sources/subversion/subversion"
                for name,digest in upstream["source_files"].items():
                    relative=name.split("/subversion/",1)[1]
                    patched=upstream_source/relative
                    if sha256(patched)!=digest:
                        raise RuntimeError("SDK Subversion source hash mismatch: "+relative)
                    target=frozen/"upstream/patched"/relative
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(patched,target)
                    if sha256(target)!=digest:
                        raise RuntimeError("SDK Subversion source snapshot mismatch: "+relative)
                private_config=upstream_source/"svn_private_config.h"
                if sha256(private_config)!=upstream["generated_private_config_sha256"]:
                    raise RuntimeError("generated Subversion private config hash mismatch")
                shutil.copy2(private_config,frozen/"upstream/svn_private_config.h")
                if sha256(frozen/"upstream/svn_private_config.h")!=upstream["generated_private_config_sha256"]:
                    raise RuntimeError("generated Subversion private config snapshot mismatch")
                for name,key in (("repos.json","repository_patch_sha256"),
                                 ("protocol.json","protocol_patch_sha256")):
                    patch=ROOT/"patches/pgvn/subversion-1.14.5"/name
                    target=frozen/"upstream/patches"/name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(patch,target)
                    if sha256(patch)!=upstream[key] or sha256(target)!=upstream[key]:
                        raise RuntimeError("Subversion patch snapshot mismatch: "+name)
        if component=="monitor":
            frozen_metadata=inputs
            frozen_manifest=manifest
        frozen_components.append((component,work,manifest,inputs,frozen))
        return {"manifest":manifest,"inputs":inputs,"sdk_manifest_sha256":sdk_hash}
    def admin(arguments, dsn):
        result=docker_command([
            "/repo/build/pgvn/linux/" + component_dir("admin") + "/bin/pgvnadmin",
            *arguments, "--config", "/repo/" + str(config.relative_to(ROOT))
        ], network=True)
        if result.returncode:
            state=subprocess.run(docker+["inspect",db,"--format",
                                  "{{.State.Status}} {{.State.ExitCode}}"],
                                 env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            logs=subprocess.run(docker+["logs",db],env=environment,stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,text=True)
            raise RuntimeError("admin command failed: " + " ".join(arguments) +
                               "\ndb_state: " + state.stdout.strip() +
                               "\ndb_log: " + logs.stdout[-4000:])
        return result
    config = artifact / "monitor.conf"
    db_started = False
    process = None
    bad_reader_process = None
    bad_reader_container = None
    holder = None
    probe_container = None
    try:
        builder_id=subprocess.check_output(
            docker+["image","inspect",args.builder_image,"--format","{{.Id}}"],env=environment,text=True,
            timeout=15).strip()
        postgres_id=subprocess.check_output(
            docker+["image","inspect",args.postgres_image,"--format","{{.Id}}"],env=environment,
            text=True,timeout=15).strip()
        check("builder image is available",bool(builder_id))
        check("PostgreSQL image is available",bool(postgres_id))
        builder_os=subprocess.check_output(
            docker+["run","--rm","--network","none",args.builder_image,
                    "sh","-c","cat /etc/os-release"],env=environment,text=True,
            timeout=30)
        build = ["python3", "scripts/pgvn/build.py", "--target", "linux",
                 "--components", "admin", "--linux-sdk", args.sdk,
                 "--configuration", args.configuration]
        if args.sanitize: build.append("--sanitize")
        docker_command(build, timeout=300)
        admin_frozen=freeze_component("admin")
        build = ["python3", "scripts/pgvn/build.py", "--target", "linux",
                 "--components", "monitor", "--linux-sdk", args.sdk,
                 "--configuration", args.configuration]
        if args.sanitize: build.append("--sanitize")
        docker_command(build, timeout=300)
        monitor_frozen=freeze_component("monitor")
        db_started=True
        subprocess.run(docker+["run", "-d", "--name", db, "--network", "none",
                        "--tmpfs", "/var/lib/postgresql:rw,size=384m",
                        "--tmpfs", "/var/run/postgresql:rw,size=16m",
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e",
                        "POSTGRES_DB=pgvn_monitor", args.postgres_image,
                        "postgres", "-c", "max_connections=64"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True,timeout=60,env=environment)
        for _ in range(120):
            ready=subprocess.run(docker+["exec",db,"psql","-w","-h","127.0.0.1",
                                  "-U","postgres","-d","pgvn_monitor","-At","-c","SELECT 1"],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10,
                                 env=environment)
            if ready.returncode==0: break
            time.sleep(.25)
        else: raise RuntimeError("PostgreSQL readiness timeout")
        version=subprocess.check_output(docker+["exec",db,"psql","-U","postgres",
                                         "-d","postgres","-At","-c",
                                         "SHOW server_version_num"],env=environment,text=True,
                                        timeout=15).strip()
        check("PostgreSQL 18.6", version=="180006")
        setup=("DROP DATABASE IF EXISTS pgvn_monitor; CREATE ROLE pgvn_owner LOGIN NOSUPERUSER; "
               "CREATE ROLE pgvn_monitor LOGIN NOSUPERUSER;"
               "CREATE DATABASE pgvn_monitor;"
               "ALTER DATABASE pgvn_monitor OWNER TO pgvn_owner;")
        setup_result=subprocess.run(docker+["exec","-i",db,"psql","-U","postgres","-d","postgres",
                                    "-v","ON_ERROR_STOP=1"],input=setup,text=True,
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,
                                    env=environment)
        if setup_result.returncode:
            raise RuntimeError("database setup failed: "+setup_result.stderr[-2000:])
        owner="host=127.0.0.1 dbname=pgvn_monitor user=pgvn_owner connect_timeout=3"
        monitor=owner.replace("pgvn_owner","pgvn_monitor")
        config.write_text("[pgvn]\nconnection = "+owner+"\nmonitor-connection = "+monitor+
                          "\nnode-id = linux-monitor-probe\n",encoding="utf-8")
        admin(["db","init"],owner)
        admin(["db","migrate"],owner)
        admin(["db","grant","--monitor-role","pgvn_monitor"],owner)
        token_result=admin(["monitor-token","create","--label","linux-monitor",
                            "--expires-in","900"],owner)
        token=json.loads(token_result.stdout)["token"]
        secrets_seen.append(token)
        expired_result=admin(["monitor-token","create","--label","linux-monitor-expired",
                              "--expires-in","1"],owner)
        expired_token=json.loads(expired_result.stdout)["token"]
        secrets_seen.append(expired_token)
        repository_result=admin(["repo","create","monitor-demo","--route",
                                 "/svn/monitor-demo"],owner)
        repository=json.loads(repository_result.stdout)
        token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest()
        stored_hash=subprocess.check_output(
            docker+["exec",db,"psql","-U","postgres","-d","pgvn_monitor","-At","-c",
             "SELECT encode(token_hash,'hex') FROM pgvn.monitor_tokens WHERE token_id='"+
             json.loads(token_result.stdout)["id"]+"'"],env=environment,text=True,timeout=15).strip()
        if stored_hash != token_digest:
            raise RuntimeError("monitor token hash mismatch")
        probe=ROOT/"build/pgvn/linux"/component_dir("monitor")/"bin/linux_monitor_probe"
        probe_container="pgvn-monitor-probe-"+uuid.uuid4().hex[:8]
        runtime_options=(["-e","ASAN_OPTIONS=abort_on_error=1:detect_leaks=1",
                          "-e","UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1"]
                         if args.sanitize else [])
        process=FramedProbe(docker+["run","-i","--rm","--name",probe_container,
                                    "--network","container:"+db,
                                    "--mount",f"type=bind,src={ROOT},dst=/repo","-w","/repo",
                                    "-e","LD_LIBRARY_PATH=/repo/build/pgvn/linux/"+
                                    component_dir("monitor")+"/lib:/repo/"+args.sdk+"/lib",
                                    *runtime_options,
                                    args.builder_image,"/repo/"+str(probe.relative_to(ROOT)),
                                    "/repo/"+str(config.relative_to(ROOT))],environment)
        owned_containers.append(probe_container)
        def send(line,timeout=10):
            return process.request(line,timeout=timeout)
        def send_raw(value,label):
            return process.request(label,raw=value)
        def wait_for(line,predicate,timeout=20):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                value=send(line,timeout=3)
                if predicate(value):
                    return value
                time.sleep(.25)
            raise RuntimeError("timed out waiting for "+line)
        def api(path,raw=None):
            body=send_raw(raw,"api invalid-utf8") if raw is not None else send("api "+path)
            match=re.match(r"^api ([0-9]+) (.*)$",body,re.S)
            if not match:
                raise RuntimeError("malformed API response: "+body[:200])
            code=int(match.group(1)); payload=match.group(2)
            obj=json.loads(payload) if code==200 else None
            return code,payload,obj
        def wait_for_overview(predicate,timeout=15):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                result=api("/api/monitor/v1/overview")
                if result[0]==200 and predicate(result[2]):
                    return result
                time.sleep(.25)
            raise RuntimeError("timed out waiting for overview state")
        def wait_for_bad_reader(reader,timeout=15):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                value=reader.request("ready",timeout=3)
                if value=="ready 0":
                    return value
                time.sleep(.25)
            raise RuntimeError("timed out waiting for restricted reader denial")
        check("initial configured state is not business-ready",
              send("ready")=="ready 0")
        check("business flag command accepted",send("business 1")=="ok")
        check("main tick command accepted",send("tick")=="ok")
        check("sampled ready state",wait_for("ready",lambda value:value=="ready 1")=="ready 1")
        check("live state after sample",send("live")=="live 1")
        overview_code,overview_payload,overview=api("/api/monitor/v1/overview")
        check("valid overview response",overview_code==200 and isinstance(overview,dict))
        repository_id=json.loads(repository_result.stdout)["id"]
        code,repositories_payload,repositories=api("/api/monitor/v1/repositories?limit=1")
        check("valid repository snapshot",code==200 and "monitor-demo" in repositories_payload)
        code,repo_payload,repo_object=api("/api/monitor/v1/repos/"+repository_id)
        check("valid repository detail",code==200 and isinstance(repo_object,dict))
        check("malformed query rejected",api("/api/monitor/v1/repositories?limit")[0]==400)
        check("duplicate query rejected",
              api("/api/monitor/v1/repositories?limit=1&limit=2")[0]==400)
        check("invalid UTF-8 query rejected",
              api(None,b"api /api/monitor/v1/repositories?q=\xff")== (400,"invalid_query",None))
        code,series_payload,series=api("/api/monitor/v1/series?metric=private_bytes")
        private_values=[item.get("value") for item in series.get("items",[])] if isinstance(series,dict) else []
        check("private-byte series contains null values",
              code==200 and bool(private_values) and all(value is None for value in private_values))
        metrics_first=send("metrics")
        check("single framed metrics response",metrics_first.count("# HELP")>=1)
        check("valid token authorization",send("authorize Bearer "+token)=="authorize 1")
        bad_token="pgvn_"+("0"*64)
        secrets_seen.append(bad_token)
        check("bad token denied",send("authorize Bearer "+bad_token)=="authorize 0")
        check("expired token denied",
              wait_for("authorize Bearer "+expired_token,
                       lambda value:value=="authorize 0",timeout=10)=="authorize 0")
        check("stalled main tick is not live",
              wait_for("live",lambda value:value=="live 0",timeout=20)=="live 0")
        check("stalled main tick is not ready",send("ready")=="ready 0")
        check("explicit tick restores liveness",send("tick")=="ok" and
              wait_for("live",lambda value:value=="live 1")=="live 1")
        check("explicit tick restores readiness",
              wait_for("ready",lambda value:value=="ready 1")=="ready 1")
        metrics_second=send("metrics")
        subprocess.run(docker+["exec",db,"psql","-U","postgres","-d","postgres",
                        "-v","ON_ERROR_STOP=1","-c",
                        "ALTER DATABASE pgvn_monitor WITH ALLOW_CONNECTIONS false"],
                       check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        subprocess.run(docker+["exec",db,"psql","-U","postgres","-d","postgres",
                        "-v","ON_ERROR_STOP=1","-c",
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname='pgvn_monitor' AND pid<>pg_backend_pid()"],
                       check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        outage_code,outage_payload,outage=wait_for_overview(
            lambda value:value.get("last_attempt_ok") is False)
        subprocess.run(docker+["exec",db,"psql","-U","postgres","-d","postgres",
                        "-v","ON_ERROR_STOP=1","-c",
                        "ALTER DATABASE pgvn_monitor WITH ALLOW_CONNECTIONS true"],
                       check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        restored_code,restored_payload,restored=wait_for_overview(
            lambda value:value.get("last_attempt_ok") is True and
            value.get("sample_id",0)>outage.get("sample_id",0) and value.get("ready") is True and
            value.get("freshness")=="fresh")
        check("database outage sampled not-ready",
              outage_code==200 and outage.get("ready") is False)
        check("database restoration sampled fresh ready",
              restored_code==200 and restored.get("sample_id",0)>outage.get("sample_id",0))
        check("token remains valid after restoration",send("authorize Bearer "+token)=="authorize 1")
        admin(["monitor-token","revoke",json.loads(token_result.stdout)["id"]],owner)
        check("revoked token denied after sampler refresh",
              wait_for("authorize Bearer "+token,lambda value:value=="authorize 0",timeout=15)=="authorize 0")
        holder=subprocess.Popen(
            docker+["exec","-i",db,"psql","-U","postgres","-d","pgvn_monitor",
             "-v","ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,env=environment)
        holder.stdin.write("BEGIN; LOCK TABLE pgvn.repositories IN ACCESS EXCLUSIVE MODE;\n")
        holder.stdin.flush()
        lock_deadline=time.monotonic()+15
        while time.monotonic()<lock_deadline:
            lock_count=subprocess.check_output(
                docker+["exec",db,"psql","-U","postgres","-d","pgvn_monitor","-At","-c",
                 "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                 "WHERE a.datname='pgvn_monitor' AND a.application_name='pgvn-monitor-read' "
                 "AND a.wait_event_type='Lock' AND l.locktype='relation' "
                 "AND l.relation='pgvn.repositories'::regclass AND NOT l.granted"],
                env=environment,text=True,timeout=15).strip()
            if lock_count=="1":
                break
            time.sleep(.1)
        else:
            raise RuntimeError("monitor sampler did not reach lock rendezvous")
        stop_started=time.monotonic()
        send("stop")
        probe_returncode=process.wait(timeout=5)
        stop_elapsed=time.monotonic()-stop_started
        session_wait_started=time.monotonic()
        for _ in range(50):
            remaining_sessions=subprocess.check_output(
                docker+["exec",db,"psql","-U","postgres","-d","postgres","-At","-c",
                 "SELECT count(*) FROM pg_stat_activity WHERE datname='pgvn_monitor' "
                 "AND application_name IN ('pgvn-monitor-read','pgvn-monitor-write')"],
                env=environment,text=True,timeout=5).strip()
            if remaining_sessions=="0":
                break
            time.sleep(.1)
        session_wait_seconds=time.monotonic()-session_wait_started
        if remaining_sessions!="0":
            lingering=subprocess.check_output(
                docker+["exec",db,"psql","-U","postgres","-d","postgres","-At","-c",
                 "SELECT coalesce(json_agg(json_build_object('pid',pid,'application_name',"
                 "application_name,'state',state,'wait_event_type',wait_event_type)),'[]'::json)::text "
                 "FROM pg_stat_activity WHERE datname='pgvn_monitor' "
                 "AND application_name IN ('pgvn-monitor-read','pgvn-monitor-write')"],
                env=environment,text=True,timeout=5).strip()
            raise RuntimeError("monitor sessions remain after stop: "+lingering)
        remaining_locks=subprocess.check_output(
            docker+["exec",db,"psql","-U","postgres","-d","pgvn_monitor","-At","-c",
             "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
             "WHERE a.datname='pgvn_monitor' "
             "AND a.application_name IN ('pgvn-monitor-read','pgvn-monitor-write')"],
            env=environment,text=True,timeout=5).strip()
        check("sampler releases all PostgreSQL locks",remaining_locks=="0")
        holder.stdin.write("ROLLBACK;\n")
        holder.stdin.close()
        holder.stdin=None
        holder_result=holder.wait(timeout=5)
        check("lock holder exits cleanly",holder_result==0)
        bad_config=artifact/"writer-reader.conf"
        bad_config.write_text("[pgvn]\nconnection = "+owner+
                              "\nmonitor-connection = "+owner+
                              "\nnode-id = linux-monitor-writer-reader\n",
                              encoding="utf-8")
        bad_reader_container="pgvn-monitor-writer-reader-"+uuid.uuid4().hex[:8]
        bad_reader_process=FramedProbe(docker+["run","-i","--rm","--name",bad_reader_container,
            "--network","container:"+db,"--mount",f"type=bind,src={ROOT},dst=/repo",
            "-w","/repo","-e","LD_LIBRARY_PATH=/repo/build/pgvn/linux/"+
            component_dir("monitor")+"/lib:/repo/"+args.sdk+"/lib",*runtime_options,
            args.builder_image,"/repo/"+str(probe.relative_to(ROOT)),
            "/repo/"+str(bad_config.relative_to(ROOT))],environment)
        owned_containers.append(bad_reader_container)
        check("wrong privileged monitor reader is rejected",
              bad_reader_process.request("business 1")=="ok" and
              wait_for_bad_reader(bad_reader_process,15)=="ready 0")
        check("wrong reader stops cleanly",bad_reader_process.request("stop")=="stopped" and
              bad_reader_process.wait(timeout=8)==0)
        secrets_seen.append(token)
        (artifact/"probe-output.txt").write_text(process.formatted_history(redact),encoding="utf-8")
        (artifact/"probe-stderr.txt").write_text(redact(process.stderr_text()),encoding="utf-8")
        (artifact/"writer-reader-output.txt").write_text(
            bad_reader_process.formatted_history(redact),encoding="utf-8")
        (artifact/"writer-reader-stderr.txt").write_text(
            redact(bad_reader_process.stderr_text()),encoding="utf-8")
        check("probe exits cleanly",probe_returncode==0)
        check("bounded stalled query shutdown",stop_elapsed < 2.0)
        check("stopped sampler releases its database session",remaining_sessions=="0")
        process_object=overview.get("process",{}) if isinstance(overview,dict) else {}
        check("private bytes null in process",process_object.get("private_bytes") is None)
        check("private bytes omitted from metrics","pgvn_process_private_bytes" not in metrics_first)
        cpu_values=[float(value) for value in re.findall(
            r"pgvn_process_cpu_seconds_total\{[^}]+\} ([0-9.]+)",
            metrics_first+"\n"+metrics_second)]
        check("CPU totals are nonnegative and monotonic",
              len(cpu_values)>=2 and all(value>=0 for value in cpu_values) and
              all(left<=right for left,right in zip(cpu_values,cpu_values[1:])))
        rss_values=[int(value) for value in re.findall(
            r"pgvn_process_working_set_bytes\{[^}]+\} ([0-9]+)",
            metrics_first+"\n"+metrics_second)]
        check("RSS samples are positive when available",bool(rss_values) and
              all(value>0 for value in rss_values))
        heartbeat_private=subprocess.check_output(
            docker+["exec",db,"psql","-U","postgres","-d","pgvn_monitor","-At","-c",
             "SELECT metrics->'private_bytes'='null'::jsonb FROM pgvn_monitor.nodes "
             "ORDER BY last_seen_at DESC LIMIT 1"],env=environment,text=True,timeout=15).strip()
        check("heartbeat private bytes are JSON null",heartbeat_private=="t")
        sanitizer_diagnostics=re.compile(
            r"AddressSanitizer|LeakSanitizer|UndefinedBehaviorSanitizer|runtime error:",
            re.IGNORECASE)
        sanitizer_output="\n".join(
            sanitizer_outputs+[process.formatted_history(redact),process.stderr_text(),
                              bad_reader_process.formatted_history(redact),
                              bad_reader_process.stderr_text()])
        if sanitizer_diagnostics.search(sanitizer_output):
            raise RuntimeError("sanitizer diagnostics detected in collected output")
        (artifact/"process.json").write_text(json.dumps(process_object,indent=2)+"\n",
                                             encoding="utf-8")
        (artifact/"metrics.prom").write_text(metrics_first,encoding="utf-8")
        final_metadata=collect(ROOT,"linux",args.configuration)
        if final_metadata!=frozen_metadata:
            raise RuntimeError("source or generated metadata changed during monitor acceptance")
        for component,work,manifest,inputs,frozen in frozen_components:
            if json.loads((work/"build-manifest.json").read_text(encoding="utf-8"))!=manifest:
                raise RuntimeError(component+" build manifest changed during acceptance")
            if json.loads((frozen/"build-manifest.json").read_text(encoding="utf-8"))!=manifest:
                raise RuntimeError(component+" frozen build manifest mismatch")
            if json.loads((frozen/"build-inputs.json").read_text(encoding="utf-8"))!=inputs:
                raise RuntimeError(component+" frozen build inputs mismatch")
            if sha256(ROOT/args.sdk/"sdk-manifest.json")!=manifest["sdk_manifest_sha256"]:
                raise RuntimeError(component+" SDK manifest changed during acceptance")
            if collect(ROOT,"linux",args.configuration)["build_identity"]!=manifest["build_identity"]:
                raise RuntimeError(component+" build identity changed during acceptance")
            for relative,digest in manifest["files"].items():
                if sha256(work/relative)!=digest:
                    raise RuntimeError(component+" product changed during acceptance: "+relative)
                if sha256(frozen/"products"/relative)!=digest:
                    raise RuntimeError(component+" frozen product hash mismatch: "+relative)
            for relative,digest in inputs["sources"].items():
                if sha256(frozen/"source"/relative)!=digest:
                    raise RuntimeError(component+" frozen source hash mismatch: "+relative)
            if sha256(frozen/"sdk-manifest.json")!=manifest["sdk_manifest_sha256"]:
                raise RuntimeError(component+" frozen SDK manifest mismatch")
            for name,digest in json.loads(
                    (frozen/"generated-hashes.json").read_text(encoding="utf-8")).items():
                if sha256(frozen/"generated"/name)!=digest:
                    raise RuntimeError(component+" frozen generated hash mismatch: "+name)
            upstream=manifest.get("upstream_inputs")
            if upstream:
                archives=list((frozen/"upstream").glob("*.tar.gz"))
                if len(archives)!=1 or sha256(archives[0])!=upstream["archive_sha256"]:
                    raise RuntimeError(component+" frozen upstream archive mismatch")
                for name,digest in upstream["source_files"].items():
                    relative=name.split("/subversion/",1)[1]
                    if sha256(frozen/"upstream/patched"/relative)!=digest:
                        raise RuntimeError(component+" frozen patched source mismatch: "+relative)
                if sha256(frozen/"upstream/svn_private_config.h")!=upstream["generated_private_config_sha256"]:
                    raise RuntimeError(component+" frozen generated upstream config mismatch")
                for name,key in (("repos.json","repository_patch_sha256"),
                                 ("protocol.json","protocol_patch_sha256")):
                    if sha256(frozen/"upstream/patches"/name)!=upstream[key]:
                        raise RuntimeError(component+" frozen patch mismatch: "+name)
        database_log=subprocess.run(docker+["logs",db],env=environment,stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,text=True,timeout=15)
        (artifact/"postgres.log").write_text(redact(database_log.stdout),encoding="utf-8")
        (artifact/"build-logs.json").write_text(
            json.dumps(build_logs,indent=2)+"\n",encoding="utf-8")
        report={"format":1,"run_id":artifact.name,"status":"PASS",
                "configuration":args.configuration,
                "sanitizers":["address","undefined"] if args.sanitize else [],
                "builder":args.builder_image,"builder_digest":builder_id,
                "builder_os":builder_os if "builder_os" in locals() else None,
                "host_os":host_os,
                "postgres_image":args.postgres_image,
                "postgres_digest":postgres_id,
                "server_version_num":version,"checks":len(checks),
                "check_names":checks,"complete_product":False,
                "sampler_sessions_after_stop":int(remaining_sessions),
                "sampler_locks_after_stop":int(remaining_locks),
                "sampler_session_release_wait_seconds":round(session_wait_seconds,3),
                "build_identity":frozen_manifest["build_identity"],
                "sdk_manifest_sha256":monitor_frozen["sdk_manifest_sha256"],
                "product_hashes":frozen_manifest["files"],
                "admin_support_identity":admin_frozen["manifest"]["build_identity"],
                "source_manifest_sha256":frozen_manifest["build_identity"]["source_manifest_sha256"],
                "generated_hashes":json.loads(
                    (artifact/"inputs/monitor/generated-hashes.json").read_text(encoding="utf-8")),
                "build_inputs_sha256":sha256(artifact/"inputs/monitor/build-inputs.json"),
                "scope":"Linux monitoring data backend/probe only; HTTP listener, web UI, telemetry-enabled service integration, Apache/DAV and deployment are out of scope",
                "limitations":["CPU uses online host CPUs, not cgroup quota","Linux RSS is approximate",
                               "private committed bytes are unavailable through this Linux implementation",
                               "initial DNS resolution may block",
                               "the initial state is configured but not business-ready; unconfigured bootstrap was not tested",
                               "real service integration remains --monitor-disabled"]}
        success_report=report
        (artifact/"reproducibility.md").write_text(
            "# Linux monitor backend run\n\n"
            f"- Run: `{artifact.name}`\n- Configuration: `{args.configuration}`"
            f"{' + ASan/UBSan' if args.sanitize else ''}\n"
            f"- Builder: `{args.builder_image}` ({builder_id})\n"
            f"- Builder OS: `{builder_os.splitlines()[0] if builder_os else 'unavailable'}`\n"
            f"- Host OS: `{host_os.splitlines()[0] if host_os else 'unavailable'}`\n"
            f"- PostgreSQL: `{args.postgres_image}` ({postgres_id}), "
            f"`server_version_num={version}`\n"
            f"- SDK manifest SHA-256: `{monitor_frozen['sdk_manifest_sha256']}`\n"
            "- Database container used `--network none`, tmpfs data directories, and no published ports.\n"
            "- Probe containers shared only the disposable database container's network namespace.\n"
            "- The real service was not started; `--monitor-disabled` remains required.\n"
            "- `complete_product` remains `false`.\n",
            encoding="utf-8")
    except Exception as error:
        message=redact(str(error))
        if db_started:
            try:
                database_log=subprocess.run(docker+["logs",db],env=environment,stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT,text=True,timeout=15)
                (artifact/"postgres.log").write_text(redact(database_log.stdout),encoding="utf-8")
            except (OSError,subprocess.TimeoutExpired):
                pass
        (artifact/"failure.txt").write_text(message+"\n",encoding="utf-8")
        (artifact/"build-logs.json").write_text(
            json.dumps(build_logs,indent=2)+"\n",encoding="utf-8")
        (artifact/"report.json").write_text(json.dumps({
            "format":1,"run_id":artifact.name,"status":"FAIL",
            "configuration":args.configuration,"sanitizers":
            ["address","undefined"] if args.sanitize else [],
            "server_version_num":locals().get("version"),
            "check_names":checks,"checks":len(checks),
            "complete_product":False,"error":message,
            "limitations":["This runner covers the Linux monitoring backend only"]
        },indent=2)+"\n",encoding="utf-8")
        raise RuntimeError(message) from None
    finally:
        if process is not None:
            try:
                process.terminate()
            except Exception as error:
                cleanup_errors.append("probe cleanup: "+str(error))
        if bad_reader_process is not None:
            try:
                bad_reader_process.terminate()
            except Exception as error:
                cleanup_errors.append("restricted reader cleanup: "+str(error))
        if holder is not None and holder.poll() is None:
            try:
                holder.stdin.write("ROLLBACK;\n")
                holder.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            holder.stdin=None
            try:
                holder.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()
        names=set(owned_containers)
        names.update(name for name in (probe_container,bad_reader_container,db) if name)
        for name in names:
            try:
                result=subprocess.run(docker+["rm","-f",name],env=environment,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      text=True,timeout=20)
                present=subprocess.run(docker+["inspect",name],env=environment,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      timeout=10)
                if present.returncode==0:
                    cleanup_errors.append("container remains after cleanup: "+name)
                elif b"no such object" not in present.stderr.lower() and b"no such container" not in present.stderr.lower():
                    cleanup_errors.append("could not verify container absence: "+name)
                elif result.returncode not in (0,1):
                    cleanup_errors.append("container cleanup failed: "+name)
            except (OSError,subprocess.TimeoutExpired) as error:
                cleanup_errors.append("container cleanup failed for "+name+": "+str(error))
        if cleanup_errors:
            failure={"format":1,"run_id":artifact.name,"status":"FAIL",
                     "configuration":args.configuration,"checks":len(checks),
                     "check_names":checks,"complete_product":False,
                     "error":"; ".join(cleanup_errors)}
            (artifact/"report.json").write_text(json.dumps(failure,indent=2)+"\n",
                                                encoding="utf-8")
            if success_report is not None:
                raise RuntimeError("; ".join(cleanup_errors))
        elif success_report is not None:
            (artifact/"report.json").write_text(
                json.dumps(success_report,indent=2)+"\n",encoding="utf-8")
            print(json.dumps({"artifact":str(artifact),"checks":len(checks),"status":"PASS"}))

if __name__=="__main__":
    main()
