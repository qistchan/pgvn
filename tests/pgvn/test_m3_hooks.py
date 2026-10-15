"""Native external hooks over two real SVN protocol nodes."""
from pathlib import Path
import ctypes
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import test_m3_credentials as setup
from test_m3_protocol_smoke import Node, SVN, PASSWORD
from fixture import ROOT, RUNTIME

WORK, EMPTY = setup.WORK, setup.EMPTY
EVENTS = WORK / "全局 hook events"
HOOKS = WORK / "全局 hook programs"
PROBE = RUNTIME / "bin/hook_probe.exe"


def cli(tool, *args, expected=0):
    result = subprocess.run([str(SVN / (tool + ".exe")), *map(str, args), "--non-interactive", "--no-auth-cache",
        "--config-dir", str(WORK / "svn-client-config"), "--username", "writer", "--password-from-stdin"],
        input=PASSWORD + b"\r\n", cwd=EMPTY, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=35)
    assert result.returncode in (expected if isinstance(expected, tuple) else (expected,)), (tool,args,result.returncode,result.stderr[:2500])
    return result


def launch(tool, *args):
    process = subprocess.Popen([str(SVN / (tool + ".exe")), *map(str,args), "--non-interactive", "--no-auth-cache",
        "--config-dir", str(WORK / "svn-client-config"), "--username", "writer", "--password-from-stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=EMPTY, creationflags=subprocess.CREATE_NO_WINDOW)
    process.stdin.write(PASSWORD+b"\r\n"); process.stdin.flush()
    return process


def wait_file(path, process, seconds=10):
    deadline = time.monotonic()+seconds
    while not path.exists() or not path.read_bytes():
        assert process.poll() is None, process.communicate()
        assert time.monotonic()<deadline, str(path)
        time.sleep(0.025)


def traces():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(EVENTS.glob("hook-*.json"))]


def alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p,ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:
        assert ctypes.get_last_error()==87, ctypes.get_last_error()
        return False
    try:
        return kernel.WaitForSingleObject(handle,0)==258
    finally:
        kernel.CloseHandle(handle)


def dead(pid):
    deadline=time.monotonic()+5
    while alive(pid):
        assert time.monotonic()<deadline, f"Owned hook process {pid} survived"
        time.sleep(0.05)


def checks():
    setup.setup_database()
    EVENTS.mkdir(); HOOKS.mkdir()
    shutil.copy2(PROBE,HOOKS/PROBE.name)
    key=WORK/"hooks.key"
    setup.admin("key","create",key,"hook-test-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repository=setup.admin("repo","create","hook-test","--route","/hooks")
    rid=repository["id"]
    rules=WORK/"hook-authz.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    definitions={}
    def handler(mode="record", *args, timeout=5):
        return {"executable":"hook_probe.exe","arguments":[mode,str(EVENTS),*map(str,args)],"timeout_seconds":timeout}
    def publish(defs, settings=None, expected=0):
        generation=setup.admin("config","show",rid)["generation"]
        path=WORK/("hooks-"+uuid.uuid4().hex+".json"); path.write_text(json.dumps(defs,ensure_ascii=False),encoding="utf-8")
        args=["config","publish",rid,"--expected-generation",generation,"--authz",rules,"--hooks",path]
        if settings is not None:
            path=WORK/("settings-"+uuid.uuid4().hex+".json"); path.write_text(json.dumps(settings),encoding="utf-8")
            args += ["--settings",path]
        value=setup.admin(*args,expected=expected)
        if expected: assert setup.admin("config","show",rid)["generation"]==generation
        return value
    event_names=("start-commit","pre-commit","post-commit","pre-revprop-change","post-revprop-change","pre-lock","post-lock","pre-unlock","post-unlock")
    literal=["",'quote " value',"尾部\\", "中文 参数", "& ; $(literal)"]
    definitions={event:handler("record",*literal) for event in event_names}
    publish(definitions)
    # Invalid definitions must not advance the published generation.
    for invalid in ({"unknown":"allow"},{"pre-commit":{"executable":"../escape.exe"}},
        {"pre-commit":{"executable":"bad.cmd"}}, {"pre-commit":handler(timeout=0)},
        {"pre-commit":{"executable":"hook_probe.exe","arguments":"bad"}},
        {"pre-commit":{"executable":"hook_probe.exe","arguments":[1]}},
        {"pre-commit":{"executable":"hook_probe.exe","arguments":[""]*17}},
        {"pre-commit":{"executable":"hook_probe.exe","unknown":"value"}}):
        publish(invalid,expected=2)
    for settings in ({"unknown":"value"},{"anonymous_access":"invalid"},{"identity_domain":"bad\nname"}):
        publish(definitions,settings,expected=2)
    a,b=Node("hook-node-a",key),Node("hook-node-b",key)
    for node in (a,b):
        with node.config.open("a",encoding="utf-8") as file: file.write("hook-root = "+HOOKS.as_posix()+"\n")
    try:
        previous={env_name:os.environ.get(env_name) for env_name in ("PGPASSWORD","PGVN_DSN")}
        try:
            os.environ["PGPASSWORD"]="HOOK_TEST_PARENT_SENTINEL"
            os.environ["PGVN_DSN"]="password=HOOK_TEST_PARENT_SENTINEL"
            a.start(); b.start()
        finally:
            for env_name,value in previous.items():
                if value is None: os.environ.pop(env_name,None)
                else: os.environ[env_name]=value
        first,second=a.url("/hooks"),b.url("/hooks")
        payload=WORK/"payload.bin"; payload.write_bytes(bytes(range(256))*513)
        cli("svnmucc","-m","hook-initial","mkdir",first+"/trunk","put",payload,first+"/trunk/file.bin")
        assert setup.admin("look","youngest",rid,role="pgvn_service")["youngest"]==1
        assert setup.run(RUNTIME/"bin/pgvnadmin.exe",["look","cat",rid,"/trunk/file.bin"],role="pgvn_service").stdout==payload.read_bytes()
        setup.admin("repo","disable",rid,role="pgvn_service",expected=5)
        prop=WORK/"binary.prop"; prop.write_bytes(b"\0\xffbinary\r\n")
        cli("svn","propset","--revprop","-r","1","pgvn:binary","--file",prop,first)
        prop.write_bytes(b"")
        cli("svn","propset","--revprop","-r","1","pgvn:binary","--file",prop,second)
        cli("svn","lock",first+"/trunk/file.bin","-m","hook lock")
        cli("svn","unlock","--force",second+"/trunk/file.bin")
        records=traces(); assert set(event_names)<= {row["event"]["event"] for row in records}
        for row in records:
            assert row["argv"][2:]==literal, row["argv"]
            assert row["event"]["repository_id"]==rid and row["event"]["username"]=="writer"
            assert row["env"]["PGVN_EVENT"]==row["event"]["event"]
            assert row["env"]["PGVN_REPOSITORY_ID"]==rid
            assert row["env"]["PGPASSWORD"] is None and row["env"]["PGVN_DSN"] is None
        import base64
        pre=[r["event"] for r in records if r["event"]["event"]=="pre-revprop-change"]
        post=[r["event"] for r in records if r["event"]["event"]=="post-revprop-change"]
        assert any(base64.b64decode(r["property_value_base64"])==b"\0\xffbinary\r\n" for r in pre)
        assert any(r["property_value_base64"]=="" for r in pre)
        assert any(r["property_value_base64"] is None for r in post)
        assert any(r["property_value_base64"] and base64.b64decode(r["property_value_base64"])==b"\0\xffbinary\r\n" for r in post)
        assert any(r["event"]["paths"]==["/trunk/file.bin"] for r in records if r["event"]["event"]=="post-lock")
        definitions["pre-lock"]=handler("token"); publish(definitions)
        cli("svn","lock",first+"/trunk/file.bin","-m","custom-token")
        assert b"opaquelocktoken:65792085-27ae-4605-acf6-b569ae68b4cc" in cli("svn","info","--xml",second+"/trunk/file.bin").stdout
        cli("svn","unlock","--force",second+"/trunk/file.bin")
        definitions["pre-lock"]=handler()
        definitions["pre-commit"]=handler("reject"); publish(definitions)
        rejected=cli("svnmucc","-m","rejected","mkdir",first+"/blocked",expected=1)
        assert b"hook rejected by test" in rejected.stderr
        assert setup.admin("look","youngest",rid)["youngest"]==1
        definitions["pre-commit"]=handler(); definitions["post-commit"]=handler("reject"); publish(definitions)
        committed=cli("svnmucc","-m","post-failure","mkdir",second+"/published")
        assert b"hook rejected by test" in committed.stdout+committed.stderr
        assert setup.admin("look","youngest",rid)["youngest"]==2
        definitions["post-commit"]=handler(); definitions["pre-commit"]=handler("flood"); publish(definitions)
        start=time.monotonic()
        flooded=cli("svnmucc","-m","flooded","mkdir",first+"/flooded",expected=1)
        assert len(flooded.stderr)<16384 and time.monotonic()-start<10
        child_pid=WORK/"hook-child.pid"
        definitions["pre-commit"]=handler("child-wait",child_pid,timeout=2); publish(definitions)
        start=time.monotonic()
        timed=cli("svnmucc","-m","timeout","mkdir",second+"/timeout",expected=1)
        elapsed=time.monotonic()-start
        assert b"timeout" in timed.stderr and 1.5<=elapsed<10
        assert child_pid.exists(); dead(int(child_pid.read_text()))
        assert setup.admin("look","youngest",rid)["youngest"]==2
        definitions["pre-commit"]=handler(); publish(definitions)
        # Revoke while a real pre hook is waiting, after the protocol's first
        # checks. The storage transaction must reject the subsequent mutation.
        for event in ("pre-revprop-change","pre-lock","pre-commit"):
            ready=EVENTS/(event+".ready"); resume=EVENTS/(event+".resume")
            definitions[event]=handler("gate",ready,resume,timeout=15); publish(definitions)
            if event=="pre-revprop-change":
                pending=launch("svn","propset","--revprop","-r","1","pgvn:binary","must-not-change",first)
            elif event=="pre-lock":
                pending=launch("svn","lock",second+"/trunk/file.bin","-m","revoked")
            else:
                pending=launch("svnmucc","-m","stale-policy","mkdir",first+"/stale-policy")
            try:
                wait_file(ready,pending)
                if event=="pre-commit":
                    hook=next(row["event"] for row in traces() if row["pid"]==int(ready.read_text()))
                    assert setup.admin("look","txn-info",rid,"--txn",hook["transaction_id"],role="pgvn_service")["author"]
                    publish(definitions)
                else: setup.admin("user","disable","pg vn test","writer")
                resume.write_text("resume")
                _,error=pending.communicate(timeout=15)
                assert pending.returncode==1 and (b"credential" in error or b"policy" in error),error[:2000]
            finally:
                if pending.poll() is None: pending.kill(); pending.communicate(timeout=10)
            if event!="pre-commit": setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
            definitions[event]=handler(); publish(definitions)
            assert setup.admin("look","youngest",rid)["youngest"]==2
        assert cli("svn","propget","--strict","--revprop","-r","1","pgvn:binary",second).stdout==b""
        assert b"<lock>" not in cli("svn","info","--xml",first+"/trunk/file.bin").stdout
        # Publish first, then lose the serving process before its post hook can
        # return the protocol reply. Another node and the receipt agree.
        ready=EVENTS/"post-commit.ready"; resume=EVENTS/"post-commit.resume"
        definitions["post-commit"]=handler("gate",ready,resume,timeout=15); publish(definitions)
        pending=launch("svnmucc","-m","reply-lost","mkdir",first+"/reply-lost")
        try:
            wait_file(ready,pending)
            assert setup.admin("look","youngest",rid)["youngest"]==3
            pid=int(ready.read_text())
            event=next(row["event"] for row in traces() if row["pid"]==pid)
            assert event["event"]=="post-commit" and event["revision"]==3 and event["transaction_id"]
            a.stop(); dead(pid)
            _,error=pending.communicate(timeout=15)
            assert pending.returncode==1,error[:2000]
        finally:
            if pending.poll() is None: pending.kill(); pending.communicate(timeout=10)
        assert b"reply-lost" in cli("svn","list",second).stdout
        receipt=setup.run(RUNTIME/"bin/core_probe.exe",["receipt",rid,"writer",event["transaction_id"]],role="pgvn_service")
        assert receipt.stdout.strip()==b"revision=3"
        setup.run(RUNTIME/"bin/core_probe.exe",["receipt",rid,"other-user",event["transaction_id"]],role="pgvn_service",expected=1)
        definitions["post-commit"]=handler(); publish(definitions)
        a.start()
        assert b"reply-lost" in cli("svn","list",first).stdout
        setup.admin("verify",rid)
        setup.admin("repo","disable",rid)
        setup.admin("look","youngest",rid,role="pgvn_service",expected=6)
        assert setup.admin("look","youngest",rid)["youngest"]==3
        setup.admin("repo","enable",rid)
        assert setup.sql("SELECT count(*) FROM pgfs.transactions WHERE state='active'")=="0"
        assert list(EMPTY.iterdir())==[]
        return {"all_nine_events":True,"json_env_argv_quoting":True,"binary_empty_null_revprop_values":True,
            "custom_pre_lock_token":True,"invalid_policy_atomic_rejection":True,
            "pre_rejection_preserves_head":True,"post_failure_preserves_published_revision":True,
            "bounded_output_draining":True,"timeout_seconds":elapsed,"timeout_kills_descendants":True,
            "inflight_revprop_and_lock_revocation":True,"inflight_commit_policy_revocation":True,
            "lost_reply_published_revision":3,"cross_node_receipt_and_history_agree":True,
            "receipt_user_isolation":True,"node_loss_kills_owned_hook":True,
            "service_role_look_history_and_active_transaction":True,"disabled_look_requires_admin":True,
            "full_verify":True,"active_transactions":0,"repository_directories":0}
    finally:
        a.stop(); b.stop()


def main():
    start=time.monotonic()
    try: report={"status":"PASS","details":checks()}
    except Exception: report={"status":"FAIL","error":traceback.format_exc()}
    report.update(test_id=setup.TEST_ID.replace("credentials","hooks"),component="M3 external hooks",milestone_complete=False,
        database=setup.DATABASE,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),elapsed_seconds=time.monotonic()-start,
        pgvnserve_sha256=hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest(),hook_probe_sha256=hashlib.sha256(PROBE.read_bytes()).hexdigest())
    target=ROOT/"artifacts/pgvn/tests"; target.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2)+"\n"
    (target/(report["test_id"]+".json")).write_text(data,encoding="utf-8")
    (target/"m3-hooks-latest.json").write_text(data,encoding="utf-8")
    print(data); return 0 if report["status"]=="PASS" else 1


if __name__=="__main__": sys.exit(main())
