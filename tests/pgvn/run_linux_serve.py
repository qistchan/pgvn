"""Build, freeze and exercise the native Linux foreground SVN service."""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import signal
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts/pgvn"))
from build_metadata import collect,sha256
from embed_migrations import CATALOG,STORAGE


def safe_arguments(arguments):
    output=[]
    redact=False
    for argument in map(str,arguments):
        if redact:
            output.append("<redacted>")
            redact=False
        else:
            output.append(argument)
            redact=argument=="--password"
    return output


def run_suite(product,artifacts):
    artifacts=Path(artifacts)
    manifest=json.loads((product/"build-manifest.json").read_text())
    frozen_build_inputs=json.loads((product/"generated/build-inputs.json").read_text())
    evidence_build_inputs=json.loads((artifacts/"inputs/build-metadata.json").read_text())
    if frozen_build_inputs!=evidence_build_inputs:
        raise RuntimeError("Frozen generated build inputs differ from the evidence snapshot")
    if frozen_build_inputs["build_identity"]!=manifest["build_identity"]:
        raise RuntimeError("Frozen generated build identity differs from the build manifest")
    for relative,expected in manifest["files"].items():
        if sha256(product/relative)!=expected:
            raise RuntimeError("Frozen serve product hash mismatch: "+relative)
    runtime=json.loads(subprocess.check_output([product/"bin/pgvnserve","--version"],text=True))
    if runtime!=manifest["runtime"]:
        raise RuntimeError("Frozen serve executable identity differs from its build manifest")
    if os.environ.get("PGVN_SERVE_DSN") is None:
        raise RuntimeError("Isolated PostgreSQL test connection was not provided")
    if os.environ.get("PGVN_EMPTY_DSN") is None:
        raise RuntimeError("Isolated empty-schema test connection was not provided")
    environment=dict(os.environ)
    environment["LC_ALL"]="C.UTF-8"
    environment["HOME"]=str(artifacts/"home")
    Path(environment["HOME"]).mkdir(exist_ok=True)
    svn_cli=product/"bin/svn"
    server=product/"bin/pgvnserve"
    fixture=product/"bin/serve_fixture"
    checks=[]
    commands=[]
    elapsed={}
    temp_root=Path(tempfile.mkdtemp(prefix="serve-",dir=artifacts))
    owned_processes=[]
    owned_clients=[]
    service_stderr_logs=[]
    config_dir=temp_root/"svn-config"
    config_dir.mkdir()

    def command(arguments,timeout=60,expected=0,input_data=None,env=None,binary=False):
        result=subprocess.run(list(map(str,arguments)),cwd=temp_root,env=env or environment,
            input=input_data,text=not binary,
            capture_output=True,timeout=timeout)
        stdout=result.stdout
        if binary:
            stdout={"length":len(stdout),"sha256":hashlib.sha256(stdout).hexdigest()}
        commands.append({"args":safe_arguments(arguments),"exit_code":result.returncode,
            "stdout":stdout if isinstance(stdout,(str,dict)) else stdout.decode("utf-8","replace"),
            "stderr":result.stderr if isinstance(result.stderr,str) else result.stderr.decode("utf-8","replace")})
        if expected is not None and result.returncode!=expected:
            raise RuntimeError("Command failed: "+str(safe_arguments(arguments))+"\n"+
                str(result.stdout)+str(result.stderr))
        return result

    def check(name,condition):
        if not condition: raise AssertionError(name)
        checks.append(name)
        print(json.dumps({"check":name,"status":"PASS"}),flush=True)

    def write_config(path,port,capacity=64,dsn=None,key_path=None):
        connection=dsn or os.environ["PGVN_SERVE_DSN"]
        key=key_path or temp_root/"credential.key"
        path.write_text("[pgvn]\nconnection = "+connection+"\ncredential-key-file = "+str(key)+
            "\nidentity-domain = pgvn\nlisten-host = 127.0.0.1\nlisten-port = "+str(port)+
            "\nmax-connections = "+str(capacity)+"\nmax-request-bytes = 67108864"+
            "\nmax-response-bytes = 67108864\nread-only = false\n",encoding="utf-8")
        path.chmod(0o600)

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            return sock.getsockname()[1]

    def start_service(config,name,timeout=15,expected_ready=True):
        output=open(artifacts/(name+"-stdout.log"),"wb")
        error_path=artifacts/(name+"-stderr.log")
        error=open(error_path,"wb")
        process=subprocess.Popen([server,"--config",config,"--monitor-disabled"],
            cwd=temp_root,env=environment,stdin=subprocess.DEVNULL,stdout=output,stderr=error)
        output.close()
        error.close()
        owned_processes.append(process)
        service_stderr_logs.append(error_path)
        deadline=time.monotonic()+timeout
        stdout_path=artifacts/(name+"-stdout.log")
        while time.monotonic()<deadline:
            if process.poll() is not None:
                if expected_ready:
                    raise RuntimeError(name+" exited before readiness: "+str(process.returncode))
                return process
            if stdout_path.exists() and b"PgVN SVN listener ready" in stdout_path.read_bytes():
                return process
            time.sleep(.05)
        if expected_ready:
            raise TimeoutError(name+" did not become ready")
        return process

    def stop_service(process,signum=signal.SIGTERM,timeout=22):
        started=time.monotonic()
        if process.poll() is None:
            try:
                process.send_signal(signum)
            except ProcessLookupError:
                pass
            try:
                code=process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise TimeoutError("Owned pgvnserve process failed to stop")
        else:
            code=process.returncode
        if code!=0:
            raise RuntimeError("Owned pgvnserve exited with status "+str(code))
        return time.monotonic()-started

    def svn_arguments(arguments):
        return [svn_cli,*arguments,"--non-interactive","--no-auth-cache","--config-dir",
            config_dir,"--username","alice","--password","serve-correct-password"]

    def svn(arguments,timeout=60,expected=0,input_data=None,binary=False):
        return command(svn_arguments(arguments),timeout=timeout,expected=expected,
            input_data=input_data,binary=binary)

    main_process=None
    try:
        check("frozen product hashes",True)
        version_result=command([server,"--version"])
        check("manifest runtime identity",json.loads(version_result.stdout)==manifest["runtime"])
        for args in (["--service"],["--monitor-url","http://127.0.0.1:18080"],["--monitor-disabled"]):
            result=command([server,*args],expected=PGVN_USAGE)
            check("unsupported CLI rejected: "+args[0],result.returncode==PGVN_USAGE)
        empty_config=temp_root/"empty-schema.conf"
        write_config(empty_config,free_port(),dsn=os.environ["PGVN_EMPTY_DSN"])
        empty_schema=start_service(empty_config,"empty-schema",timeout=4,expected_ready=False)
        check("empty database startup is classified as schema mismatch",
            empty_schema.wait(timeout=5)==PGVN_SCHEMA)
        port=free_port()
        config=temp_root/"service.conf"
        write_config(config,port)
        fixture_env=dict(environment,PGVN_SERVE_DSN=os.environ["PGVN_SERVE_DSN"])
        result=command([fixture,temp_root/"credential.key","serve-test","/serve-test","alice"],timeout=60,env=fixture_env)
        repository=json.loads(result.stdout)
        check("public API repository, key, credential and policy fixture",bool(repository["repository_id"]))
        url="svn://127.0.0.1:"+str(port)+repository["route"]
        main_process=start_service(config,"serve-main")
        info=svn(["info",url])
        check("authenticated SVN info",info.returncode==0 and "Revision: 0" in info.stdout)
        wrong=command([svn_cli,"info",url,"--non-interactive","--no-auth-cache","--config-dir",
            config_dir,"--username","alice","--password","wrong-password"],expected=None)
        check("wrong password denied",wrong.returncode!=0 and "Authentication" in wrong.stderr)
        first=temp_root/"checkout-one"
        second=temp_root/"checkout-two"
        svn(["checkout",url,first])
        svn(["checkout",url,second])
        payload=b"pgvn Linux native svn protocol: exact bytes \x00\xff\n"
        (first/"payload.bin").write_bytes(payload)
        svn(["add",first/"payload.bin"])
        svn(["commit",first,"-m","native listener commit"])
        svn(["update",second])
        cat=svn(["cat",url+"/payload.bin"],binary=True)
        check("checkout, commit, update and byte-matching cat",cat.stdout==payload and
              (second/"payload.bin").read_bytes()==payload)
        request=artifacts/"inflight-lock.request"
        held=artifacts/"inflight-lock.held"
        request.write_text("hold PgFS transaction work during an SVN commit\n")
        deadline=time.monotonic()+15
        while not held.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        if not held.exists():
            raise TimeoutError("Outer runner did not hold the PgFS transaction table lock")
        inflight_payload=os.urandom(512*1024)
        (second/"inflight.bin").write_bytes(inflight_payload)
        svn(["add",second/"inflight.bin"])
        inflight_args=svn_arguments(["commit",second,"-m","in-flight shutdown commit"])
        inflight=subprocess.Popen(list(map(str,inflight_args)),cwd=temp_root,env=environment,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        owned_clients.append(inflight)
        command_request=artifacts/"inflight-command.request"
        command_blocked=artifacts/"inflight-command.blocked"
        command_request.write_text("wait until the PgVN commit is blocked on the held transaction-table lock\n")
        deadline=time.monotonic()+15
        while not command_blocked.exists() and time.monotonic()<deadline:
            if inflight.poll() is not None:
                raise RuntimeError("Large SVN commit completed before the lock rendezvous")
            time.sleep(.05)
        if not command_blocked.exists():
            raise TimeoutError("Outer runner did not observe the PgVN commit blocked on pgfs.transactions")
        signal_time=time.monotonic()
        main_process.send_signal(signal.SIGTERM)
        try:
            client_stdout,client_stderr=inflight.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            inflight.kill()
            client_stdout,client_stderr=inflight.communicate(timeout=5)
            raise TimeoutError("In-flight SVN commit did not finish within shutdown grace")
        client_finished=time.monotonic()
        commands.append({"args":safe_arguments(inflight_args),"exit_code":inflight.returncode,
            "stdout":client_stdout,"stderr":client_stderr})
        service_code=main_process.wait(timeout=5)
        service_finished=time.monotonic()
        elapsed["inflight_commit_seconds"]=client_finished-signal_time
        elapsed["inflight_service_shutdown_seconds"]=service_finished-signal_time
        print(json.dumps({"check":"in-flight commit shutdown details","client_exit":inflight.returncode,
            "client_seconds":elapsed["inflight_commit_seconds"],"service_exit":service_code,
            "service_seconds":elapsed["inflight_service_shutdown_seconds"]}),flush=True)
        check("in-flight commit succeeds during shutdown grace",inflight.returncode==0 and
              client_finished>=signal_time+2 and client_finished<signal_time+10 and service_code==0 and
              service_finished<=signal_time+10)
        owned_clients.remove(inflight)
        main_process=None
        recovered=start_service(config,"serve-after-inflight-drain")
        cat=svn(["cat",url+"/inflight.bin"],binary=True)
        check("in-flight transaction remains readable after drain",cat.stdout==inflight_payload)
        stop_service(recovered,signal.SIGTERM)
        recovered=None
        for name,invalid_port,capacity,key in (
            ("invalid-port",70000,64,None),("invalid-capacity",port,65,None),
            ("invalid-key",port,64,temp_root/"missing.key")):
            invalid=temp_root/(name+".conf")
            write_config(invalid,invalid_port,capacity,key_path=key)
            failed=start_service(invalid,name,timeout=4,expected_ready=False)
            code=failed.wait(timeout=5)
            expected=PGVN_DENIED if name=="invalid-key" else PGVN_USAGE
            check(name+" startup exit class",code==expected)
        malformed=temp_root/"invalid-config.conf"
        malformed.write_text("[pgvn\n",encoding="utf-8")
        failed=start_service(malformed,"invalid-config",timeout=4,expected_ready=False)
        check("invalid configuration startup exit class",failed.wait(timeout=5)==PGVN_USAGE)
        collision_owner=start_service(config,"serve-collision-owner")
        duplicate=start_service(config,"serve-collision",timeout=4,expected_ready=False)
        code=duplicate.wait(timeout=5)
        check("bind collision is classified as conflict",code==PGVN_CONFLICT)
        stop_service(collision_owner)
        capacity_config=temp_root/"capacity.conf"
        capacity_port=free_port()
        write_config(capacity_config,capacity_port,1)
        capacity_server=start_service(capacity_config,"serve-capacity")
        first_client=socket.create_connection(("127.0.0.1",capacity_port),timeout=3)
        time.sleep(.2)
        second_client=socket.create_connection(("127.0.0.1",capacity_port),timeout=3)
        second_client.settimeout(2)
        check("session capacity rejects overflow",second_client.recv(1)==b"")
        second_client.close()
        first_client.close()
        time.sleep(.4)
        reused=socket.create_connection(("127.0.0.1",capacity_port),timeout=3)
        reused.settimeout(2)
        accepted=reused.recv(1)!=b""
        check("completed worker slot is reused",accepted)
        reused.close()
        time.sleep(.4)
        drain_client=socket.create_connection(("127.0.0.1",capacity_port),timeout=3)
        drain_client.settimeout(2)
        if drain_client.recv(1)==b"":
            raise RuntimeError("Stalled client was rejected before the drain test")
        duration=stop_service(capacity_server)
        elapsed["stalled_client_shutdown_seconds"]=duration
        check("stalled client drains after grace",duration>=9 and duration<22)
        drain_client.close()
        capacity_server=start_service(capacity_config,"serve-port-reuse")
        stop_service(capacity_server,signal.SIGINT,timeout=5)
        capacity_server=None
        recovered=start_service(config,"serve-after-drain")
        def handshake_and_disconnect(_):
            with socket.create_connection(("127.0.0.1",port),timeout=3) as client:
                client.settimeout(3)
                return bool(client.recv(1))
        for _ in range(3):
            with ThreadPoolExecutor(max_workers=16) as executor:
                handshakes=list(executor.map(handshake_and_disconnect,range(16)))
            if not all(handshakes):
                raise RuntimeError("Concurrent SVN handshake was not accepted")
        info=svn(["info",url])
        check("concurrent disconnect and slot reuse followed by SVN info",
            len(handshakes)==16 and info.returncode==0)
        stop_service(recovered,signal.SIGTERM)
        recovered=None
        check("listener port reused and committed transaction remains readable",True)
        wrong_db=temp_root/"retry.conf"
        write_config(wrong_db,free_port(),dsn="host=127.0.0.1 port=1 dbname=missing user=pgvn_owner")
        retry=start_service(wrong_db,"serve-retry",timeout=3,expected_ready=False)
        stopped_at=time.monotonic()
        stop_service(retry,signal.SIGTERM,timeout=3)
        elapsed["startup_retry_stop_seconds"]=time.monotonic()-stopped_at
        check("database retry wait is interruptible",elapsed["startup_retry_stop_seconds"]<2.5)
        request=artifacts/"db-recovery.request"
        paused=artifacts/"db-recovery.paused"
        released=artifacts/"db-recovery.released"
        request.write_text("pause isolated PostgreSQL for retry recovery\n")
        deadline=time.monotonic()+15
        while not paused.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        if not paused.exists():
            raise TimeoutError("Outer runner did not pause isolated PostgreSQL")
        recovery_started=time.monotonic()
        recovery=start_service(config,"serve-db-recovery",timeout=30)
        elapsed["database_recovery_readiness_seconds"]=time.monotonic()-recovery_started
        check("database retry recovers to readiness",elapsed["database_recovery_readiness_seconds"]<30 and
              released.exists())
        svn(["info",url])
        stop_service(recovery,signal.SIGTERM)
        recovery=None
    finally:
        active_exception=sys.exc_info()[0] is not None
        cleanup_errors=[]
        for process in owned_clients:
            if process.poll() is None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                except Exception as error:
                    cleanup_errors.append("client cleanup: "+str(error))
        for process in owned_processes:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception as error:
                        cleanup_errors.append("service kill: "+str(error))
                except ProcessLookupError:
                    try:
                        process.wait(timeout=5)
                    except Exception as error:
                        cleanup_errors.append("service reap: "+str(error))
                except Exception as error:
                    cleanup_errors.append("service cleanup: "+str(error))
        sanitizer_markers=("ERROR: AddressSanitizer","ERROR: LeakSanitizer",
            "LeakSanitizer: detected memory leaks","UndefinedBehaviorSanitizer","runtime error:")
        diagnostic_logs=[]
        for path in service_stderr_logs:
            if path.exists():
                diagnostic_logs.append((str(path),path.read_text(errors="replace")))
        diagnostic_logs.extend((str(item.get("args","<command>")),str(item.get("stderr","")))
            for item in commands)
        sanitizer_diagnostics=[name+": "+marker for name,text in diagnostic_logs
            for marker in sanitizer_markers if marker in text]
        if sanitizer_diagnostics:
            cleanup_errors.extend(sanitizer_diagnostics)
        if cleanup_errors:
            commands.append({"cleanup_errors":cleanup_errors})
        (artifacts/"svn-commands.jsonl").write_text("".join(json.dumps(item)+"\n" for item in commands))
        shutil.rmtree(temp_root,ignore_errors=True)
        if cleanup_errors and not active_exception:
            raise RuntimeError("; ".join(cleanup_errors))
    return {"checks":checks,"elapsed_seconds":elapsed,"runtime":runtime,"commands":commands,
        "limitations":["This suite validates the native foreground listener and production svn:// protocol/core only; Linux pgvnadmin, monitoring, Apache/DAV, packaging, deployment setup and full Linux product acceptance remain outside scope.",
            "Telemetry-enabled serve operation was not validated; this host currently initializes telemetry as disabled."]}


PGVN_USAGE=2
PGVN_SCHEMA=4
PGVN_DENIED=5
PGVN_CONFLICT=7


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk",type=Path,required=True)
    parser.add_argument("--configuration",choices=("Release","Debug"),default="Release")
    parser.add_argument("--sanitize",action="store_true")
    parser.add_argument("--builder-image",default="pgvn-linux-builder:24.04")
    parser.add_argument("--postgres-image",default="postgres:18.6")
    parser.add_argument("--in-container",action="store_true")
    parser.add_argument("--product",type=Path)
    parser.add_argument("--artifacts",type=Path)
    args=parser.parse_args()
    if args.in_container:
        result=run_suite(args.product.resolve(strict=True),args.artifacts.resolve(strict=True))
        report={"format":1,"status":"PASS","scope":"Native Linux foreground svn:// listener against isolated pinned PostgreSQL 18.6",
            "checks":result["checks"],"elapsed_seconds":result["elapsed_seconds"],"runtime":result["runtime"],
            "limitations":result["limitations"]}
        path=args.artifacts/"serve-suite-report.json"
        path.write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps({"report":str(path),"checks":len(result["checks"]),"status":"PASS"}),flush=True)
        return 0

    run_id="linux-serve-"+uuid.uuid4().hex[:12]
    test_container_name=run_id+"-suite"
    artifacts=ROOT/"artifacts/pgvn/linux-native-serve"/run_id
    artifacts.mkdir(parents=True)
    environment=dict(os.environ)
    for name in ("DOCKER_HOST","DOCKER_CONTEXT","DOCKER_TLS","DOCKER_TLS_VERIFY","DOCKER_CERT_PATH"):
        environment.pop(name,None)
    docker=["docker","--host=unix:///var/run/docker.sock"]
    commands=[]
    errors=[]
    postgres_started=False
    postgres_paused=False
    metadata=None
    product=None
    builder_os=None
    host_os=Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else platform.platform()

    def command(arguments,timeout=1800,data=None,check=True,extra_env=None):
        env=dict(environment)
        if extra_env: env.update(extra_env)
        result=subprocess.run(list(map(str,arguments)),cwd=ROOT,env=env,input=data,text=True,
            capture_output=True,timeout=timeout)
        commands.append({"args":safe_arguments(arguments),"exit_code":result.returncode,
            "stdin_sha256":hashlib.sha256(data.encode()).hexdigest() if data is not None else None,
            "stdout":result.stdout,"stderr":result.stderr})
        if check and result.returncode:
            raise RuntimeError("Command failed: "+str(safe_arguments(arguments))+"\n"+result.stdout+result.stderr)
        return result

    def run_test_with_recovery(arguments,timeout,extra_env):
        nonlocal postgres_paused
        env=dict(environment,**extra_env)
        process=None
        output_path=artifacts/"tests.log"
        try:
            with output_path.open("wb") as output_file:
                process=subprocess.Popen(list(map(str,arguments)),cwd=ROOT,env=env,
                    stdout=output_file,stderr=subprocess.STDOUT)
                deadline=time.monotonic()+timeout
                request=artifacts/"db-recovery.request"
                lock_request=artifacts/"inflight-lock.request"
                command_request=artifacts/"inflight-command.request"
                lock_started=False
                command_checked=False
                while process.poll() is None and time.monotonic()<deadline:
                    if lock_request.exists() and not lock_started:
                        lock_command=docker+["exec","-d",run_id,"psql","-h","127.0.0.1","-X",
                            "-v","ON_ERROR_STOP=1","-U","postgres","-d","pgvn_serve","-c",
                            "BEGIN; LOCK TABLE pgfs.transactions IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(5); COMMIT;"]
                        command(lock_command,timeout=30)
                        holder_sql=("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                            "WHERE a.datname=current_database() AND a.pid<>pg_backend_pid() "
                            "AND l.locktype='relation' AND l.relation='pgfs.transactions'::regclass "
                            "AND l.mode='AccessExclusiveLock' AND l.granted;")
                        lock_deadline=time.monotonic()+15
                        while time.monotonic()<lock_deadline:
                            query=command(docker+["exec",run_id,"psql","-h","127.0.0.1","-X","-At",
                                "-U","postgres","-d","pgvn_serve","-c",holder_sql],timeout=10)
                            if int(query.stdout.strip()):
                                break
                            time.sleep(.05)
                        else:
                            raise TimeoutError("Isolated PostgreSQL did not hold pgfs.transactions exclusively")
                        (artifacts/"inflight-lock.held").write_text(holder_sql+"\n")
                        lock_started=True
                    if command_request.exists() and not command_checked:
                        blocked_sql=("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                            "WHERE a.datname=current_database() AND a.application_name='pgvn' "
                            "AND a.pid<>pg_backend_pid() AND l.locktype='relation' "
                            "AND l.relation='pgfs.transactions'::regclass AND NOT l.granted "
                            "AND a.wait_event_type='Lock';")
                        blocked_deadline=time.monotonic()+15
                        while time.monotonic()<blocked_deadline:
                            query=command(docker+["exec",run_id,"psql","-h","127.0.0.1","-X","-At",
                                "-U","postgres","-d","pgvn_serve","-c",blocked_sql],timeout=10)
                            if int(query.stdout.strip()):
                                (artifacts/"inflight-command.blocked").write_text(blocked_sql+"\n")
                                break
                            time.sleep(.05)
                        else:
                            diagnostics=command(docker+["exec",run_id,"psql","-h","127.0.0.1","-X","-At",
                                "-F","\t","-U","postgres","-d","pgvn_serve","-c",
                                "SELECT pid,application_name,state,wait_event_type,wait_event,query "
                                "FROM pg_stat_activity WHERE datname=current_database() "
                                "AND pid<>pg_backend_pid() ORDER BY pid;"],timeout=10,check=False)
                            (artifacts/"inflight-command-diagnostics.txt").write_text(
                                "predicate:\n"+blocked_sql+"\nactivity:\n"+diagnostics.stdout+diagnostics.stderr)
                            raise TimeoutError("PgVN command was not observed waiting on the held relation lock; see inflight-command-diagnostics.txt")
                        command_checked=True
                    if request.exists() and not postgres_paused:
                        command(docker+["pause",run_id],timeout=30)
                        postgres_paused=True
                        (artifacts/"db-recovery.paused").write_text("PostgreSQL paused\n")
                        try:
                            time.sleep(4)
                        finally:
                            command(docker+["unpause",run_id],timeout=30)
                            postgres_paused=False
                            (artifacts/"db-recovery.released").write_text("PostgreSQL resumed\n")
                    time.sleep(.05)
                if process.poll() is None:
                    raise TimeoutError("Native serve test runner exceeded its timeout")
                code=process.wait(timeout=5)
                output=output_path.read_text(errors="replace")
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            command(docker+["rm","-f",test_container_name],timeout=30,check=False)
        output=output_path.read_text(errors="replace")
        record={"args":safe_arguments(arguments),"exit_code":process.returncode,
            "stdout":output,"stderr":""}
        commands.append(record)
        if process.returncode:
            raise RuntimeError("Command failed: "+str(record["args"])+"\n"+output)
        return subprocess.CompletedProcess(arguments,process.returncode,output,"")

    def check(name,condition):
        if not condition: raise AssertionError(name)
        print(json.dumps({"check":name,"status":"PASS"}),flush=True)

    try:
        sdk=args.sdk.resolve(strict=True)
        image_ids={}
        for image in (args.builder_image,args.postgres_image):
            image_ids[image]=command(docker+["image","inspect","--format","{{.Id}}",image]).stdout.strip()
        builder_os=command(docker+["run","--rm","--network","none",args.builder_image,
            "sh","-c","cat /etc/os-release"]).stdout
        build_arguments=["python3","scripts/pgvn/build.py","--target","linux","--components","serve",
            "--configuration",args.configuration,"--linux-sdk",sdk]
        if args.sanitize: build_arguments.append("--sanitize")
        build_docker=["docker","--host=unix:///var/run/docker.sock","run","--rm","--network","none",
            "--user",str(os.getuid())+":"+str(os.getgid()),"--memory","3g","--cpus","3",
            "--mount","type=bind,src="+str(ROOT)+",dst="+str(ROOT),"-w",ROOT,
            args.builder_image,*build_arguments]
        built=command(build_docker,timeout=2400)
        (artifacts/"build.log").write_text(built.stdout+built.stderr)
        build_result=json.loads(built.stdout.strip().splitlines()[-1])
        work=Path(build_result["output"])
        build_manifest=json.loads((work/"build-manifest.json").read_text())
        metadata=collect(ROOT,"linux",args.configuration)
        generated_inputs=json.loads((work/"generated/build-inputs.json").read_text())
        if generated_inputs!=metadata:
            raise RuntimeError("Current collected metadata differs from generated build-inputs.json")
        if build_manifest["build_identity"]!=metadata["build_identity"]:
            raise RuntimeError("Current collected build identity differs from the build manifest")
        sdk_manifest_source=sdk/"sdk-manifest.json"
        sdk_manifest_hash=sha256(sdk_manifest_source)
        if sdk_manifest_hash!=build_manifest["sdk_manifest_sha256"]:
            raise RuntimeError("SDK manifest differs from the native serve build input")
        product=artifacts/"frozen-product"
        shutil.copytree(work,product,ignore=shutil.ignore_patterns("*.o","commands.jsonl"))
        manifest=json.loads((product/"build-manifest.json").read_text())
        check("built runtime files match build manifest",all(sha256(product/name)==value
            for name,value in manifest["files"].items()))
        if json.loads((product/"generated/build-inputs.json").read_text())!=metadata:
            raise RuntimeError("Frozen generated build inputs differ from pre-freeze metadata")
        shutil.copytree(product/"generated",artifacts/"inputs/generated")
        (artifacts/"inputs/build-metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")
        sdk_manifest_copy=artifacts/"inputs/sdk-manifest.json"
        shutil.copy2(sdk_manifest_source,sdk_manifest_copy)
        if sha256(sdk_manifest_copy)!=sdk_manifest_hash:
            raise RuntimeError("SDK manifest changed while freezing test inputs")
        for relative,expected in metadata["sources"].items():
            source=ROOT/relative
            target=artifacts/"inputs/source"/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if sha256(target)!=expected: raise RuntimeError("Source changed while freezing inputs: "+relative)
        for directory,names in ((ROOT/"sql",STORAGE),(ROOT/"sql/pgvn",CATALOG)):
            prefix="storage" if directory==ROOT/"sql" else "catalog"
            for name in names:
                source=directory/name
                target=artifacts/"inputs/migrations"/prefix/name
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
                if sha256(target)!=sha256(source):
                    raise RuntimeError("Migration changed while freezing inputs: "+name)
        upstream_manifest=manifest["upstream_inputs"]
        upstream_source=sdk/"sources/subversion/subversion"
        for name,expected in upstream_manifest["source_files"].items():
            relative=name.split("/subversion/",1)[1]
            source=upstream_source/relative
            target=artifacts/"inputs/upstream-source"/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if sha256(target)!=expected:
                raise RuntimeError("Subversion input changed while freezing: "+relative)
        private_config=upstream_source/"svn_private_config.h"
        private_copy=artifacts/"inputs/upstream-source/svn_private_config.h"
        shutil.copy2(private_config,private_copy)
        if sha256(private_copy)!=upstream_manifest["generated_private_config_sha256"]:
            raise RuntimeError("Generated Subversion private config changed while freezing")
        for name in ("repos.json","protocol.json"):
            source=ROOT/"patches/pgvn/subversion-1.14.5"/name
            target=artifacts/"inputs/patches"/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if sha256(target)!=sha256(source):
                raise RuntimeError("Subversion patch specification changed while freezing")
        (artifacts/"inputs/build-manifest.json").write_bytes((product/"build-manifest.json").read_bytes())
        check("pinned PostgreSQL image",image_ids[args.postgres_image])
        command(docker+["run","-d","--name",run_id,"--label","pgvn.component=linux-native-serve",
            "--network","none","--read-only","--memory","384m","--cpus","1",
            "--tmpfs","/var/lib/postgresql:rw,nosuid,noexec,size=384m",
            "--tmpfs","/var/run/postgresql:rw,nosuid,noexec,size=16m",
            "--tmpfs","/tmp:rw,nosuid,noexec,size=16m","-e","POSTGRES_HOST_AUTH_METHOD=trust",
            "-e","POSTGRES_DB=pgvn_serve",args.postgres_image,"postgres","-c","shared_buffers=32MB",
            "-c","max_connections=32"])
        postgres_started=True
        deadline=time.monotonic()+60
        while True:
            ready=command(docker+["exec",run_id,"pg_isready","-h","127.0.0.1","-U","postgres",
                "-d","pgvn_serve"],timeout=10,check=False)
            if ready.returncode==0: break
            if time.monotonic()>deadline: raise TimeoutError("PostgreSQL readiness timed out")
            time.sleep(.2)
        version=command(docker+["exec",run_id,"psql","-h","127.0.0.1","-X","-At","-U","postgres",
            "-d","pgvn_serve","-c","SHOW server_version_num;"]).stdout.strip()
        check("PostgreSQL 18.6",version=="180006")
        psql=docker+["exec","-i",run_id,"psql","-h","127.0.0.1","-X","-v","ON_ERROR_STOP=1",
            "-At","-U","postgres","-d","pgvn_serve"]
        command(psql,data="CREATE ROLE pgvn_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_serve TO pgvn_owner;")
        command(docker+["exec",run_id,"createdb","-h","127.0.0.1","-U","postgres",
            "-O","pgvn_owner","pgvn_serve_empty"])
        from build_metadata import sha256 as file_sha256
        from embed_migrations import CATALOG as catalog,STORAGE as storage
        for directory,names in ((ROOT/"sql",storage),(ROOT/"sql/pgvn",catalog)):
            for name in names:
                migration=directory/name
                command(psql,data="SET ROLE pgvn_owner; SELECT set_config('pgvn.migration_sha256','"+
                    file_sha256(migration)+"',false);\n"+migration.read_text())
        migrated=command(psql,data="SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.schema_versions;").stdout.strip()
        check("all catalog migrations applied by non-superuser",migrated=="1,2,3,4,5,6,7,8")
        dsn="host=127.0.0.1 port=5432 dbname=pgvn_serve user=pgvn_owner connect_timeout=2"
        builder_test=["docker","--host=unix:///var/run/docker.sock","run",
            "--name",test_container_name,
            "--network","container:"+run_id,"--user",str(os.getuid())+":"+str(os.getgid()),
            "--memory","1g","--cpus","2","--read-only","--tmpfs","/tmp:rw,nosuid,nodev,exec,size=64m",
            "--mount","type=bind,src="+str(ROOT)+",dst="+str(ROOT),
            "-w",ROOT,"-e","PGVN_SERVE_DSN","-e","PGVN_EMPTY_DSN",
            "-e","ASAN_OPTIONS=detect_leaks=1:abort_on_error=1",
            "-e","UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1",args.builder_image,
            "python3","tests/pgvn/run_linux_serve.py","--in-container","--sdk",sdk,
            "--configuration",args.configuration,"--product",product,"--artifacts",artifacts]
        empty_dsn="host=127.0.0.1 port=5432 dbname=pgvn_serve_empty user=pgvn_owner connect_timeout=2"
        tested=run_test_with_recovery(builder_test,timeout=240,
            extra_env={"PGVN_SERVE_DSN":dsn,"PGVN_EMPTY_DSN":empty_dsn})
        report=json.loads((artifacts/"serve-suite-report.json").read_text())
        check("native serve integration suite",report["status"]=="PASS")
        check("source and migration inventory unchanged",collect(ROOT,"linux",args.configuration)==metadata)
    except Exception:
        errors.append(traceback.format_exc())
        print(errors[-1],file=sys.stderr,flush=True)
    finally:
        try:
            command(docker+["rm","-f",test_container_name],timeout=30,check=False)
        except Exception:
            errors.append(traceback.format_exc())
        if postgres_started:
            try:
                if postgres_paused:
                    command(docker+["unpause",run_id],timeout=30,check=False)
                    postgres_paused=False
                log=command(docker+["logs",run_id],timeout=30,check=False)
                (artifacts/"postgres.log").write_text(log.stdout+log.stderr)
                command(docker+["rm","-f",run_id],timeout=30,check=False)
            except Exception:
                errors.append(traceback.format_exc())
        (artifacts/"orchestration-commands.jsonl").write_text("".join(json.dumps(item)+"\n" for item in commands))
        result={"format":1,"run_id":run_id,"created_at":dt.datetime.now(dt.timezone.utc).isoformat(),
            "status":"PASS" if not errors else "FAIL","scope":"Native Linux foreground SVN listener and production protocol/core",
            "configuration":args.configuration,"sanitizers":["address","undefined"] if args.sanitize else [],
            "image_ids":image_ids if "image_ids" in locals() else {},"build_manifest":str(product/"build-manifest.json") if product else None,
            "host":{"platform":platform.platform(),"machine":platform.machine(),"os_release":host_os},
            "compiler":manifest.get("compiler") if product else None,
            "builder_os_release":builder_os,"sdk_manifest_sha256":sha256(args.sdk.resolve()/ "sdk-manifest.json"),
            "source_snapshot":str(artifacts/"inputs"),"checks":json.loads((artifacts/"serve-suite-report.json").read_text()).get("checks",[]) if (artifacts/"serve-suite-report.json").exists() else [],
            "errors":errors,"artifacts":str(artifacts),"complete_product":False}
        reproducibility=["# Native Linux serve run",
            "",
            "- Status: "+result["status"],
            "- Host: "+result["host"]["platform"]+"; "+result["host"]["os_release"].strip().replace("\n","; "),
            "- Builder image: "+args.builder_image+" ("+result["image_ids"].get(args.builder_image,"unavailable")+")",
            "- Builder OS: "+(builder_os or "unavailable").strip().replace("\n","; "),
            "- PostgreSQL image: "+args.postgres_image+" ("+result["image_ids"].get(args.postgres_image,"unavailable")+")",
            "- SDK manifest SHA-256: "+result["sdk_manifest_sha256"],
            "- PostgreSQL was isolated in a `--network none` container with tmpfs data/socket/temp directories, no published ports, and `POSTGRES_HOST_AUTH_METHOD=trust`.",
            "- Catalog migrations were applied after `SET ROLE pgvn_owner`; the role was created NOSUPERUSER.",
            "- Compiler: "+str(result["compiler"]),
            "- The native listener uses no host systemd service or cgroup delegation; no cgroup setup steps were used. Supabase/shared databases and frozen hook-runtime evidence were not accessed or modified.",
            "- The runner does not pull images; it records immutable local image IDs for the builder and PostgreSQL image.",
            "- Product, generated metadata, source inputs and `sdk-manifest.json` were frozen below `inputs/` and `frozen-product/` before the suite.",
            "",
            "## Commands",
            ""]
        reproducibility += ["```sh\n"+shlex.join(safe_arguments(item["args"]))+"\n```"
            for item in commands if item["args"] and item["args"][0]=="docker"]
        reproducibility += ["",
            "Raw orchestration output is in `orchestration-commands.jsonl`; SVN commands are in `svn-commands.jsonl`; build/test output is in `build.log` and `tests.log`; database output is in `postgres.log`.",
            "The isolated service fixture uses trust authentication only inside the disposable local test container; database connection strings and credential arguments are not retained in command logs.",
            "Final case output and timing are in `serve-suite-report.json`; this is component evidence, not full Linux product acceptance."]
        (artifacts/"reproducibility.md").write_text("\n".join(reproducibility)+"\n")
        (artifacts/"report.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps({"report":str(artifacts/"report.json"),"status":result["status"],
            "checks":len(result["checks"]),"artifacts":str(artifacts)}),flush=True)
    return 1 if errors else 0


if __name__=="__main__":
    raise SystemExit(main())
