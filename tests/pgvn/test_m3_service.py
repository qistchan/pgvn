"""Temporary LocalService: real SVN, hook cancellation, drain and port reuse."""
import datetime as dt
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid
import test_m0 as bootstrap
from test_m3_hooks import setup,WORK,EMPTY,EVENTS,HOOKS,PROBE,PASSWORD,Node,cli,launch,wait_file,dead
from fixture import ROOT,RUNTIME


def checkpoints(name):
    text=bootstrap.command(["sc.exe","query",name])
    match=re.search(r"CHECKPOINT\s*:\s*(0x[0-9a-f]+|\d+)",text,re.I)
    assert match,text
    return int(match.group(1),0)


def stop_service(name):
    if bootstrap.service_state(name)!=3: bootstrap.command(["sc.exe","stop",name])
    start=time.monotonic(); observed=[]
    while bootstrap.service_state(name)!=1:
        observed.append(checkpoints(name))
        assert time.monotonic()-start<25,"Temporary PgVN service did not drain"
        time.sleep(0.15)
    return time.monotonic()-start,observed


def checks():
    setup.setup_database(); bootstrap.WORK=WORK
    EVENTS.mkdir(); HOOKS.mkdir(); shutil.copy2(PROBE,HOOKS/PROBE.name)
    key=WORK/"service.key"; setup.admin("key","create",key,"service-test-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repository=setup.admin("repo","create","service-test","--route","/service")
    rid=repository["id"]
    rules=WORK/"service-authz.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    def publish(definitions):
        generation=setup.admin("config","show",rid)["generation"]
        path=WORK/("service-hooks-"+uuid.uuid4().hex+".json"); path.write_text(json.dumps(definitions),encoding="utf-8")
        setup.admin("config","publish",rid,"--expected-generation",generation,"--authz",rules,"--hooks",path)
    publish({})
    a,b=Node("service-node",key),Node("service-peer",key)
    for node in (a,b):
        with node.config.open("a",encoding="utf-8") as file: file.write("hook-root = "+HOOKS.as_posix()+"\n")
    def rejected_config(text,expected):
        path=WORK/("bad-"+uuid.uuid4().hex+".ini"); path.write_text(text,encoding="utf-8")
        result=subprocess.run([str(RUNTIME/"bin/pgvnserve.exe"),"--config",str(path),"--monitor-url",
            f"http://127.0.0.1:{a.monitor_port}/"],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
        assert result.returncode==expected,(expected,result.returncode,result.stderr)
        assert b"PGVN_TEST_SECRET" not in result.stdout+result.stderr
    valid=a.config.read_text(encoding="utf-8")
    for value in ("-1","18446744073709551616","junk"):
        rejected_config(valid+"max-request-bytes = "+value+"\n",2)
    rejected_config(valid.replace(setup.dsn("pgvn_service"),"host=127.0.0.1 port=1 user=absent password=PGVN_TEST_SECRET"),3)
    rejected_config(valid.replace(key.as_posix(),(WORK/"missing.key").as_posix()),5)
    setup.sql("INSERT INTO pgvn.schema_versions(version,migration_sha256) VALUES(99,repeat('0',64))")
    try: rejected_config(valid,4)
    finally: setup.sql("DELETE FROM pgvn.schema_versions WHERE version=99")
    # Read-only deployment configuration/programs; only the fixture's event
    # directory is writable. The protected key gets an explicit read ACE.
    bootstrap.command(["icacls.exe",str(WORK),"/grant","*S-1-5-19:(OI)(CI)(RX)"])
    bootstrap.command(["icacls.exe",str(EVENTS),"/grant","*S-1-5-19:(OI)(CI)(M)"])
    bootstrap.command(["icacls.exe",str(key),"/grant","*S-1-5-19:(R)"])
    name="PgVN_M3_"+uuid.uuid4().hex[:12]
    url=f"http://127.0.0.1:{a.monitor_port}/"
    created=reserved=False; cleanup=[]; sockets=[]; pending=None
    try:
        bootstrap.command(["netsh.exe","http","add","urlacl","url="+url,"sddl=D:(A;;GX;;;LS)"]); reserved=True
        binary=f'"{RUNTIME/"bin/pgvnserve.exe"}" --service --service-name {name} --config "{a.config}" --monitor-url {url}'
        bootstrap.command(["sc.exe","create",name,"binPath=",binary,"obj=","NT AUTHORITY\\LocalService","start=","demand"]); created=True
        assert "LocalService" in bootstrap.command(["sc.exe","qc",name])
        b.start()
        first,second=a.url("/service"),b.url("/service")
        bootstrap.command(["sc.exe","start",name]); bootstrap.wait_service(name,4); bootstrap.wait_live(a.monitor_port)
        payload=WORK/"service-payload.bin"; payload.write_bytes(bytes(range(256))*257+b"\0\xff")
        cli("svnmucc","-m","local-service","mkdir",first+"/initial","put",payload,first+"/initial/file.bin")
        assert cli("svn","cat",first+"/initial/file.bin").stdout==payload.read_bytes()
        working_copy=WORK/"service-working-copy"
        cli("svn","checkout",first,working_copy)
        updated=payload.read_bytes()+b"service delta update\0\xff"
        (working_copy/"initial/file.bin").write_bytes(updated)
        cli("svn","commit",working_copy,"-m","service-delta")
        assert cli("svn","cat",second+"/initial/file.bin").stdout==updated
        assert b"initial" in cli("svn","list",second).stdout
        # Cancel a sixty-second pre hook and all of its descendants on STOP.
        child_pid=EVENTS/"service-hook-child.pid"
        publish({"pre-commit":{"executable":"hook_probe.exe","arguments":["child-wait",str(EVENTS),str(child_pid)],"timeout_seconds":60}})
        pending=launch("svnmucc","-m","cancel-on-stop","mkdir",first+"/must-not-publish")
        wait_file(child_pid,pending)
        cancelled_seconds,_=stop_service(name)
        _,error=pending.communicate(timeout=10)
        assert pending.returncode==1,error[:2000]
        assert cancelled_seconds<8,cancelled_seconds
        dead(int(child_pid.read_text()))
        assert setup.admin("look","youngest",rid)["youngest"]==2
        assert b"initial" in cli("svn","list",second).stdout
        publish({})
        bootstrap.command(["sc.exe","start",name]); bootstrap.wait_service(name,4); bootstrap.wait_live(a.monitor_port)
        cli("svnmucc","-m","restart-service","mkdir",first+"/after-restart")
        # Fill all session slots with clients that never send a protocol tuple.
        # HTTP stays responsive; STOP must report progress then cancel sockets.
        for _ in range(16):
            connection=socket.create_connection(("127.0.0.1",a.svn_port),timeout=3)
            connection.settimeout(3)
            assert connection.recv(4096)
            sockets.append(connection)
        assert bootstrap.request(a.monitor_port)[0]==200
        drain_seconds,observed=stop_service(name)
        assert 9<=drain_seconds<18,drain_seconds
        assert max(observed,default=0)>=3,observed
        for connection in sockets: connection.close()
        sockets=[]
        # The same SVN and HTTP ports are reusable after active drain.
        bootstrap.command(["sc.exe","start",name]); bootstrap.wait_service(name,4); bootstrap.wait_live(a.monitor_port)
        cli("svnmucc","-m","reuse-drained-ports","mkdir",first+"/after-drain")
        assert b"after-drain" in cli("svn","list",second).stdout
        final_stop_seconds,_=stop_service(name)
        assert final_stop_seconds<5,final_stop_seconds
        setup.admin("verify",rid)
        assert setup.sql("SELECT count(*) FROM pgfs.transactions WHERE state='active'")=="0"
        return {"account":"NT AUTHORITY\\LocalService","database_role":"pgvn_service","real_svn_commit":True,
            "server_startup_exit_classes":[2,3,4,5],"service_binary_checkout_delta_update":True,
            "peer_stays_available":True,"hook_cancel_seconds":cancelled_seconds,"hook_descendants_killed":True,
            "idle_svn_sessions":16,"drain_seconds":drain_seconds,"stop_checkpoints":observed,
            "same_port_restart_cycles":3,"final_idle_stop_seconds":final_stop_seconds,"full_verify":True,"active_transactions":0}
    finally:
        for connection in sockets: connection.close()
        if pending and pending.poll() is None: pending.kill(); pending.communicate(timeout=10)
        b.stop()
        try:
            if created:
                if bootstrap.service_state(name)!=1: stop_service(name)
                bootstrap.command(["sc.exe","delete",name]); cleanup.append("temporary_service_deleted")
        finally:
            if reserved:
                bootstrap.command(["netsh.exe","http","delete","urlacl","url="+url]); cleanup.append("temporary_urlacl_deleted")
            (WORK/"service-cleanup.json").write_text(json.dumps(cleanup))
        assert len(cleanup)==int(created)+int(reserved),cleanup


def main():
    start=time.monotonic()
    try: report={"status":"PASS","details":checks()}
    except Exception: report={"status":"FAIL","error":traceback.format_exc()}
    report.update(test_id=setup.TEST_ID.replace("credentials","service"),component="M3 business Windows service",milestone_complete=False,
        database=setup.DATABASE,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),elapsed_seconds=time.monotonic()-start,
        pgvnserve_sha256=hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest())
    if (WORK/"service-cleanup.json").exists(): report["cleanup"]=json.loads((WORK/"service-cleanup.json").read_text())
    target=ROOT/"artifacts/pgvn/tests"; target.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2)+"\n"
    (target/(report["test_id"]+".json")).write_text(data,encoding="utf-8")
    (target/"m3-service-latest.json").write_text(data,encoding="utf-8")
    print(data); return 0 if report["status"]=="PASS" else 1


if __name__=="__main__": sys.exit(main())
