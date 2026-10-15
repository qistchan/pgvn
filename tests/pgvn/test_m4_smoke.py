"""M4 development components: API isolation, sampling, auth and business ledger.

This is not a complete M4 milestone gate (UI/resource/fault/overhead checks follow).
"""
import datetime as dt
import hashlib
import http.client
import json
import secrets
import re
import time
import traceback
import test_m3_credentials as setup
from test_m3_protocol_smoke import Node, client, PASSWORD
from fixture import ROOT, RUNTIME

WORK=setup.WORK
TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-smoke")


def request(node,path="/api/monitor/v1/overview",token=None,expected=200,headers=None,method="GET",body=None):
    values=dict(headers or {})
    if token: values["Authorization"]="Bearer "+token
    connection=http.client.HTTPConnection("127.0.0.1",node.monitor_port,timeout=3)
    try:
        connection.request(method,path,headers=values,body=body)
        response=connection.getresponse(); payload=response.read()
        assert response.status==expected,(path,expected,response.status,payload[:1000])
        metadata=dict(response.getheaders())
        if response.getheader("Content-Type","").startswith("application/json"):
            try: return json.loads(payload),metadata
            except json.JSONDecodeError as error: raise AssertionError(payload[:3000]) from error
        return payload,metadata
    finally: connection.close()


def until(predicate,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value: return value
        time.sleep(.2)
    raise AssertionError("Timed out waiting for expected monitor state")


def checks():
    setup.setup_database()
    key=WORK/"monitor.key"; setup.admin("key","create",key,"monitor-smoke-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repo=setup.admin("repo","create","monitor-main","--route","/monitor-main")
    rules=WORK/"authz.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation","1","--authz",rules)
    for i in range(3): setup.admin("repo","create",f"monitor-page-{i}","--route",f"/page-{i}")
    # A database owner can store hostile display text; the API must preserve it
    # as data, and browser acceptance will verify it is never executed.
    hostile='<img src=x onerror="window.pgvnXss=true">'
    setup.sql("UPDATE pgvn.repositories SET canonical_name='"+hostile+"' WHERE canonical_name='monitor-page-0'")
    issued=setup.admin("monitor-token","create","--label","M4 isolated test","--expires-in","300")
    token=issued["token"]
    assert setup.sql("SELECT has_table_privilege('pgvn_monitor','pgvn.credentials','SELECT')")=="f"
    assert setup.sql("SELECT has_table_privilege('pgvn_monitor','pgvn.monitor_tokens','SELECT')")=="f"
    assert setup.sql("SELECT has_table_privilege('pgvn_service','pgvn_monitor.nodes','INSERT')")=="f"
    assert setup.sql("SELECT has_function_privilege('pgvn_monitor','pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb)','EXECUTE')")=="f"
    a,b=Node("monitor-a",key),Node("monitor-b",key)
    for node in (a,b):
        with node.config.open("a",encoding="utf-8") as out: out.write("node-id = "+node.name+"\n")
    try:
        a.start(); b.start()
        until(lambda: ready_status(a)==200)
        overview,_=request(a,token=token)
        assert overview["ready"] and overview["freshness"]=="fresh" and overview["database"]["writable"]
        assert overview["database"]["repositories"]==4
        assert overview["process"]["commands"]==0
        html,headers=request(a,"/monitor/")
        assert 'lang="zh-CN"' in html.decode() and headers["X-Content-Type-Options"]=="nosniff"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        for asset in ("monitor.css","monitor.js"): assert request(a,"/monitor/"+asset)[0]
        failure,headers=request(a,expected=401)
        assert failure["error"]["request_id"] and headers["WWW-Authenticate"].startswith("Bearer")
        request(a,token="pgvn_"+"0"*64,expected=401)
        request(a,token=token,headers={"Origin":"https://hostile.invalid"},expected=403)
        request(a,token=token,headers={"Origin":f"http://127.0.0.1:{a.monitor_port}"})
        request(a,token=token,method="POST",body=b"change",expected=405)
        request(a,token=token,body=b"ignored",expected=413)
        request(a,"/api/monitor/v1/not-real",token,expected=404)
        for query in ("limit=201","limit=-1","limit=4294967297","limit=2&limit=3","token="+token):
            payload,_=request(a,"/api/monitor/v1/repositories?"+query,token,expected=400)
            assert token not in json.dumps(payload)
        page,_=request(a,"/api/monitor/v1/repositories?limit=2",token)
        assert len(page["items"])==2 and page["next_cursor"]
        next_page,_=request(a,"/api/monitor/v1/repositories?limit=2&cursor="+page["next_cursor"],token)
        assert len(next_page["items"])==2 and next_page["next_cursor"] is None
        assert {r["id"] for r in page["items"]}.isdisjoint(r["id"] for r in next_page["items"])
        assert hostile in [r["name"] for r in page["items"]+next_page["items"]]
        until(lambda: len(request(a,"/api/monitor/v1/nodes",token)[0]["items"])==2)
        content=WORK/"metrics.bin"; content.write_bytes(secrets.token_bytes(65537))
        client("svnmucc","-m","ledger-one","put",content,a.url("/monitor-main/blob"))
        assert client("svn","cat",b.url("/monitor-main/blob"))==content.read_bytes()
        snapshot,_=request(a,token=token)
        assert snapshot["process"]["published_revisions"]==1 and snapshot["process"]["receipt_confirmations"]==0
        assert snapshot["process"]["commands"]>0
        requests,_=request(a,"/api/monitor/v1/requests",token)
        assert sum(item[k] for item in requests["items"] for k in ("success","denied","error"))==snapshot["process"]["commands"]
        metrics,_=request(a,"/metrics",token)
        assert b'pgvn_published_revisions_total' in metrics and token.encode() not in metrics
        labels=re.findall(rb'([a-z_][a-z0-9_]*)="([^"\n]*)"',metrics)
        assert all(key in {b'component',b'instance_id',b'operation',b'result',b'sqlstate_class',b'direction',b'le'} for key,value in labels)
        assert all(value not in {b'monitor-main',b'writer',token.encode()} for key,value in labels)
        until(lambda: request(a,token=token)[0]["database"]["active_transactions"]==0)
        # Interrupt only this suite's database; restoration uses postgres.
        setup.fixture.assert_ours()
        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS false",database="postgres")
        try:
            setup.fixture.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{setup.DATABASE}'",database="postgres")
            degraded=until(lambda: (lambda o:o if not o["last_attempt_ok"] else None)(request(a,token=token)[0]),seconds=12)
            assert not degraded["ready"] and degraded["process"]["commands"]==snapshot["process"]["commands"]
            request(a,"/health/live"); request(a,"/health/ready",expected=503)
            stale=until(lambda: (lambda o:o if o["freshness"]=="stale" else None)(request(a,token=token)[0]),seconds=20)
            assert stale["database"] is not None and stale["age_seconds"]>15
        finally: setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS true",database="postgres")
        until(lambda: request(a,token=token)[0]["ready"],seconds=15)
        assert b"blob" in client("svn","list",a.url("/monitor-main"))
        setup.admin("monitor-token","revoke",issued["id"])
        until(lambda: unauthorized(a,token),seconds=10)
        setup.admin("verify",repo["id"])
        return {"schema":5,"readonly_role_boundaries":True,"token_origin_methods_queries":True,
            "pagination_and_hostile_text":True,"two_node_heartbeats":True,"publication_command_ledger":True,
            "database_disconnect_live_and_stale_snapshot":True,"recovery":True,"token_revocation":True,
            "full_verify":True,"milestone_complete":False}
    finally: a.stop(); b.stop()


def ready_status(node):
    connection=http.client.HTTPConnection("127.0.0.1",node.monitor_port,timeout=2)
    try:
        connection.request("GET","/health/ready"); response=connection.getresponse(); response.read(); return response.status
    finally: connection.close()


def unauthorized(node,token):
    connection=http.client.HTTPConnection("127.0.0.1",node.monitor_port,timeout=2)
    try:
        connection.request("GET","/api/monitor/v1/overview",headers={"Authorization":"Bearer "+token})
        response=connection.getresponse(); response.read(); return response.status==401
    finally: connection.close()


def main():
    start=time.monotonic()
    try: details=checks(); status="PASS"; error=None
    except Exception: details={}; status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 development API and sampler","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,
        "elapsed_seconds":time.monotonic()-start,"pgvnserve_sha256":hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests"; output.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2)+"\n"; (output/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (output/"m4-smoke-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True); return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
