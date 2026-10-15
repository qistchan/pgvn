"""M4 component: a TCP blackhole cannot trap live HTTP or service shutdown."""
import datetime as dt
import hashlib
import json
import select
import socket
import threading
import time
import traceback
import uuid
import test_m3_credentials as setup
import test_m0 as bootstrap
from test_m3_protocol_smoke import Node,port,PASSWORD
from test_m4_smoke import request,ready_status,until
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-network")


class Proxy:
    def __init__(self):
        self.port=port(); self.stop=threading.Event(); self.blackhole=threading.Event()
        self.listener=socket.socket(); self.listener.bind(("127.0.0.1",self.port)); self.listener.listen(16); self.listener.settimeout(.2)
        self.workers=[]; self.sockets=[]; self.lock=threading.Lock()
        self.thread=threading.Thread(target=self.accept,daemon=True); self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try: client,_=self.listener.accept()
            except socket.timeout: continue
            except OSError: break
            thread=threading.Thread(target=self.forward,args=(client,),daemon=True)
            self.workers.append(thread); thread.start()

    def forward(self,client):
        upstream=None
        try:
            upstream=socket.create_connection(("127.0.0.1",16432),timeout=2)
            client.settimeout(2); upstream.settimeout(2)
            with self.lock: self.sockets.extend((client,upstream))
            while not self.stop.is_set():
                if self.blackhole.is_set(): time.sleep(.05); continue
                ready,_,_=select.select((client,upstream),(),(),.1)
                for source in ready:
                    chunk=source.recv(65536)
                    if not chunk: return
                    (upstream if source is client else client).sendall(chunk)
        except OSError: pass
        finally:
            client.close()
            if upstream: upstream.close()

    def close(self):
        self.stop.set(); self.listener.close(); self.thread.join(timeout=3)
        with self.lock:
            for connection in self.sockets:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                connection.close()
        for worker in self.workers: worker.join(timeout=3)
        assert not self.thread.is_alive() and not any(w.is_alive() for w in self.workers)


def stop_service(name,phase="service"):
    if bootstrap.service_state(name)==1: return 0.0
    start=time.monotonic(); bootstrap.command(["sc.exe","stop",name])
    while bootstrap.service_state(name)!=1:
        assert time.monotonic()-start<12,f"Timed out stopping {phase}"
        time.sleep(.1)
    return time.monotonic()-start


def checks():
    setup.setup_database(); bootstrap.WORK=setup.WORK
    setup.fixture.assert_ours()
    key=setup.WORK/"network.key"; setup.admin("key","create",key,"network-test-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    token=setup.admin("monitor-token","create","--label","network-test","--expires-in",180)["token"]
    proxy=Proxy(); node=Node("network-node",key)
    node.config.write_text(node.config.read_text(encoding="utf-8").replace("port=16432",f"port={proxy.port}"),encoding="utf-8")
    bootstrap.command(["icacls.exe",str(setup.WORK),"/grant","*S-1-5-19:(OI)(CI)(RX)"])
    bootstrap.command(["icacls.exe",str(key),"/grant","*S-1-5-19:(R)"])
    url=f"http://127.0.0.1:{node.monitor_port}/"; name="PgVN_M4_"+uuid.uuid4().hex[:12]
    created=reserved=False; cleanup=[]; slow=[]
    try:
        bootstrap.command(["netsh.exe","http","add","urlacl","url="+url,"sddl=D:(A;;GX;;;LS)"]); reserved=True
        binary=f'"{RUNTIME/"bin/pgvnserve.exe"}" --service --service-name {name} --config "{node.config}" --monitor-url {url}'
        bootstrap.command(["sc.exe","create",name,"binPath=",binary,"obj=","NT AUTHORITY\\LocalService","start=","demand"]); created=True
        bootstrap.command(["sc.exe","start",name]); bootstrap.wait_service(name,4); bootstrap.wait_live(node.monitor_port)
        until(lambda: ready_status(node)==200)
        overview,_=request(node,token=token); assert overview["ready"]
        # Preserve both ends of established TCP connections while dropping all
        # traffic. This differs from a clean disconnect or a refused connect.
        proxy.blackhole.set(); started=time.monotonic()
        degraded=until(lambda: (lambda value:value if not value["last_attempt_ok"] else None)(request(node,token=token)[0]),seconds=12)
        failure_seconds=time.monotonic()-started
        request(node,"/health/live"); request(node,"/health/ready",expected=503)
        assert degraded["process"]["commands"]==0 and degraded["database"] is not None
        stop_seconds=stop_service(name,"TCP blackhole")
        assert stop_seconds<8,stop_seconds
        checks.partial={"blackhole_keeps_tcp_open":True,"sampler_failure_seconds":failure_seconds,"blackhole_stop_seconds":stop_seconds,"live_200_ready_503_during_blackhole":True}
        # Restore the transport and reuse the exact same URL/service. Slow
        # response consumers must also be cancelled at shutdown.
        proxy.blackhole.clear()
        bootstrap.command(["sc.exe","start",name]); bootstrap.wait_service(name,4); bootstrap.wait_live(node.monitor_port)
        until(lambda: ready_status(node)==200)
        restarted,_=request(node,token=token)
        setup.sql("INSERT INTO pgvn_monitor.nodes(instance_id,node_id,component,version,process_id,started_at,last_seen_at,metrics) "
            "SELECT md5('m4-network-node-'||i)::uuid,'fixture-'||i,'svn','test',1,clock_timestamp(),clock_timestamp(),"
            "jsonb_build_object('padding',repeat('x',3800)) FROM generate_series(1,200) i")
        until(lambda: len(request(node,"/api/monitor/v1/nodes?limit=200",token)[0]["items"])==200)
        for _ in range(2):
            connection=socket.socket(); connection.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024); connection.settimeout(3)
            connection.connect(("127.0.0.1",node.monitor_port))
            connection.sendall((f"GET /api/monitor/v1/nodes?limit=200 HTTP/1.1\r\nHost: 127.0.0.1:{node.monitor_port}\r\nAuthorization: Bearer {token}\r\n\r\n").encode())
            header=connection.recv(1024); assert b"200" in header.split(b"\r\n",1)[0],header[:100]
            slow.append(connection)
        slow_stop_seconds=stop_service(name,"slow HTTP responses")
        assert slow_stop_seconds<8,slow_stop_seconds
        assert setup.sql("SELECT metrics->>'stopped' FROM pgvn_monitor.nodes WHERE instance_id='"+restarted["instance_id"]+"'")=="true"
        return {"account":"NT AUTHORITY\\LocalService","blackhole_keeps_tcp_open":True,"sampler_failure_seconds":failure_seconds,
            "live_200_ready_503_during_blackhole":True,"cached_readonly_auth_during_outage":True,"blackhole_stop_seconds":stop_seconds,
            "same_url_restarted":True,"slow_response_clients":2,"slow_response_stop_seconds":slow_stop_seconds,
            "graceful_stop_status_published":True,
            "proxy_threads_joined":True}
    finally:
        for connection in slow: connection.close()
        try:
            if created:
                # Restore forwarding before recovery cleanup, even on failure.
                proxy.blackhole.clear(); stop_service(name); bootstrap.command(["sc.exe","delete",name]); cleanup.append("temporary_service_deleted")
        finally:
            try:
                if reserved: bootstrap.command(["netsh.exe","http","delete","urlacl","url="+url]); cleanup.append("temporary_urlacl_deleted")
            finally: proxy.close(); checks.cleanup=cleanup


def main():
    start=time.monotonic()
    try: details=checks(); status="PASS"; error=None
    except Exception: details=getattr(checks,"partial",{}); status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 network and cancellation","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,"cleanup":getattr(checks,"cleanup",[]),
        "elapsed_seconds":time.monotonic()-start,"pgvnserve_sha256":hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests"; output.mkdir(parents=True,exist_ok=True); data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (output/"m4-network-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True); return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
