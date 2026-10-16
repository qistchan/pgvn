"""Monitor API contract against real SVN traffic and independently counted TCP."""
import datetime as dt
import hashlib
import json
import math
import secrets
import time
import traceback
import uuid
from urllib.parse import quote
import test_m3_credentials as setup
from test_m3_protocol_smoke import Node,client,PASSWORD,check_admission
from test_m4_smoke import request,ready_status,until
from fixture import ROOT,RUNTIME
from tcp_proxy import TcpProxy

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-api")


def capacity(node,token,expected):
    overview,_=request(node,token=token)
    assert overview["process"]["sessions_limit"]==expected,overview["process"]
    # Business database leases and SVN transport sessions have different bounds.
    assert overview["process"]["connection_limit"]==32
    metrics,_=request(node,"/metrics",token)
    sample=f'pgvn_sessions_limit{{instance_id="{overview["instance_id"]}"}} {expected}'
    assert sample in metrics.decode("utf-8").splitlines(),sample
    return overview["instance_id"]


def checks():
    setup.setup_database();setup.fixture.assert_ours()
    key=setup.WORK/"api.key";setup.admin("key","create",key,"api-test-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repo=setup.admin("repo","create","API 工程","--route","/api-main")
    rules=setup.WORK/"rules.conf";rules.write_text("[/]\nwriter = rw\n[API 工程:/forbidden]\nwriter = r\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    token=setup.admin("monitor-token","create","--label","api","--expires-in",300)["token"]
    a,b=Node("api-a",key),Node("api-b",key);proxy=TcpProxy(a.svn_port)
    with b.config.open("a",encoding="utf-8") as out:out.write("max-connections = 2\nnode-id = api-b\n")
    try:
        a.start();b.start();until(lambda:ready_status(a)==200)
        overview,_=request(a,token=token)
        assert overview["database"]["reader_restricted"] and overview["database"]["reader_read_only"]
        assert overview["process"]["commands"]==0 and overview["process"]["command_p95_upper_bound_ms"] is None
        assert setup.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn-monitor-read' AND usename='pgvn_monitor'")=="2"
        default_instance=capacity(a,token,64);default_admission=check_admission(a,64)
        until(lambda:ready_status(b)==200)
        configured_instance=capacity(b,token,2);configured_admission=check_admission(b,2)
        for search in ("api", "工程", repo["id"].upper(),repo["uuid"]):
            page,_=request(a,"/api/monitor/v1/repos?q="+quote(search,safe=""),token)
            assert [r["id"] for r in page["items"]]==[repo["id"]],search
        assert not request(a,"/api/monitor/v1/repos?q=nonexistent",token)[0]["items"]
        for query in ("q=%00","q=%ff","q=%zz","q=x&q=y","cursor=-1:2","cursor=1%3a-1"):
            request(a,"/api/monitor/v1/repos?"+query,token,expected=400)
        request(a,"/api/monitor/v1/repos/bad",token,expected=400)
        request(a,"/api/monitor/v1/repos/"+str(uuid.uuid4()),token,expected=404)
        detail,_=request(a,"/api/monitor/v1/repos/"+repo["id"],token)
        assert detail["repository"]["head_rev"]==0 and detail["recent_revisions"]["items"][0]["rev"]==0
        for metric in ("commands_per_second","error_percent","mean_latency_ms","cpu_percent","working_set_bytes","private_bytes","bytes_received","bytes_sent","sql_calls","sampler_ms"):
            series,_=request(a,"/api/monitor/v1/series?metric="+metric+"&window=300&step=15",token)
            assert series["metric"]==metric and series["window_seconds"]==300 and series["step_seconds"]==15
            assert all(item["value"] is None or math.isfinite(item["value"]) for item in series["items"])
        for query in ("metric=arbitrary","window=901","step=17","window=60&window=300","q=secret"):
            request(a,"/api/monitor/v1/series?"+query,token,expected=400)
        # The monitor itself may advance independently; business SQL must not
        # change when only cached HTTP endpoints are read.
        before,_=request(a,token=token)
        for _ in range(50):request(a,"/api/monitor/v1/repos",token)
        after,_=request(a,token=token)
        assert after["process"]["sql_calls"]==before["process"]["sql_calls"]
        assert after["process"]["monitor_requests"]-before["process"]["monitor_requests"]==51
        assert after["sampler_sql_calls"]-before["sampler_sql_calls"]<=14
        # Every SVN byte below passes through an independent raw TCP counter.
        url=a.url("/api-main").replace(str(a.svn_port),str(proxy.port),1)
        before,_=request(a,token=token);payload=setup.WORK/"payload.bin";payload.write_bytes(secrets.token_bytes(131079))
        client("svnmucc","-m","one","put",payload,url+"/blob")
        assert client("svn","cat",url+"/blob")==payload.read_bytes()
        client("svnmucc","-m","denied","mkdir",url+"/forbidden",expected=1)
        proxy.idle();until(lambda:request(a,token=token)[0]["process"]["sessions_active"]==0)
        current,_=request(a,token=token);process=current["process"]
        assert process["bytes_received"]-before["process"]["bytes_received"]==proxy.received
        assert process["bytes_sent"]-before["process"]["bytes_sent"]==proxy.sent
        assert process["published_revisions"]==1 and process["commands_active"]==0 and process["writers_active"]==0
        assert process["connections_leased"]==1 and process["sql_active"]==0
        assert sum(process["sqlstate_classes"].values())==process["sql_calls"]
        events,_=request(a,"/api/monitor/v1/events?repo="+repo["id"]+"&limit=1",token)
        assert events["items"] and events["items"][0]["repository_id"]==repo["id"]
        assert events["items"][0]["request_id"].startswith("svn-")
        until(lambda:request(a,"/api/monitor/v1/repos/"+repo["id"],token)[0]["repository"]["head_rev"]==1)
        current_detail,_=request(a,"/api/monitor/v1/repos/"+repo["id"],token)
        assert current_detail["repository"]["last_commit_at"] and current_detail["recent_revisions"]["items"][0]["rev"]==1
        # Corrupt only a disposable revision-date value: monitoring must return
        # unknown metadata instead of throwing on invalid UTF-8 or NUL bytes.
        original=setup.sql("SELECT encode(value,'hex') FROM pgfs.revision_properties WHERE rev=1 AND name='svn:date'")
        try:
            setup.sql("UPDATE pgfs.revision_properties SET value=decode('ff00','hex') WHERE rev=1 AND name='svn:date'")
            until(lambda:request(a,"/api/monitor/v1/repos/"+repo["id"],token)[0]["repository"]["last_commit_at"] is None)
            assert ready_status(a)==200
        finally:setup.sql("UPDATE pgfs.revision_properties SET value=decode('"+original+"','hex') WHERE rev=1 AND name='svn:date'")
        # An enabled repository without a route is an actual configuration
        # readiness failure, even while the primary database remains writable.
        try:
            setup.sql("DELETE FROM pgvn.routes WHERE repository_id='"+repo["id"]+"'")
            until(lambda:ready_status(a)==503)
            assert request(a,token=token)[0]["reason"]=="configuration_unavailable"
        finally:setup.sql("INSERT INTO pgvn.routes(route_prefix,repository_id) VALUES('/api-main','"+repo["id"]+"')")
        until(lambda:ready_status(a)==200)
        measured,_=request(a,token=token)
        assert measured["process"]["working_set_bytes"]>0 and measured["process"]["private_bytes"]>0
        assert measured["process"]["cpu_percent_of_machine"] is not None
        # Reject accidental use of a role that can read credential material.
        # This grant affects only the disposable test database and is restored.
        try:
            setup.sql("GRANT SELECT ON pgvn.credentials TO pgvn_monitor")
            until(lambda:request(a,token=token)[0]["reason"]=="reader_role_not_restricted")
            assert ready_status(a)==503
        finally:setup.sql("REVOKE SELECT ON pgvn.credentials FROM pgvn_monitor")
        until(lambda:ready_status(a)==200)
        # Recent errors are a finite ring, and pagination refuses an evicted
        # cursor rather than silently returning an unrelated event page.
        for _ in range(8):request(a,"/api/monitor/v1/repos?limit=201",token,expected=400)
        older,_=request(a,"/api/monitor/v1/events?limit=2",token)
        assert len(older["items"])==2 and older["next_cursor"]
        second,_=request(a,"/api/monitor/v1/events?limit=2&cursor="+older["next_cursor"],token)
        assert not {e["sequence"] for e in older["items"]}.intersection(e["sequence"] for e in second["items"])
        for _ in range(205):request(a,"/api/monitor/v1/repos?limit=201",token,expected=400)
        capped,_=request(a,"/api/monitor/v1/events?limit=200",token)
        assert len(capped["items"])==200
        request(a,"/api/monitor/v1/events?cursor="+older["next_cursor"],token,expected=409)
        # Editing the file does not alter a running listener. Only a new
        # instance may advertise and enforce the changed capacity.
        config=b.config.read_text(encoding="utf-8")
        assert config.count("max-connections = 2\n")==1
        b.config.write_text(config.replace("max-connections = 2\n","max-connections = 8\n"),encoding="utf-8")
        assert capacity(b,token,2)==configured_instance
        check_admission(b,2)
        b.stop();b.start();until(lambda:ready_status(b)==200)
        restarted_instance=capacity(b,token,8);assert restarted_instance!=configured_instance
        restarted_admission=check_admission(b,8)
        def heartbeat_capacity():
            nodes,_=request(a,"/api/monitor/v1/nodes",token)
            return any(n["instance_id"]==restarted_instance and n["metrics"].get("sessions_limit")==8 for n in nodes["items"])
        until(heartbeat_capacity)
        setup.admin("verify",repo["id"])
        return {"reader_role":"pgvn_monitor","readonly_connection":True,"search_and_repository_detail":True,
            "bounded_series_and_validation":True,"http_count_delta":51,"no_business_sql_on_http_refresh":True,
            "wire_bytes_received":proxy.received,"wire_bytes_sent":proxy.sent,"exact_socket_byte_accounting":True,
            "sql_result_classes_balance":True,"repository_error_correlation":True,"malformed_metadata_is_unknown":True,
            "configuration_readiness":True,"windows_resources":True,"event_ring_and_evicted_cursor":True,
            "overprivileged_reader_rejected":True,"full_verify":True,
            "session_capacity":{"default":64,"configured":2,"after_restart":8,
                "default_instance":default_instance,"configured_instance":configured_instance,"restarted_instance":restarted_instance,
                "default_admission":default_admission,"configured_admission":configured_admission,"restarted_admission":restarted_admission,
                "pending_config_keeps_effective_limit":True,"api_prometheus_and_heartbeat_agree":True,"database_lease_limit_unchanged":32}}
    finally:proxy.close();a.stop();b.stop()


def main():
    start=time.monotonic()
    try:details=checks();status="PASS";error=None
    except Exception:details={};status="FAIL";error=traceback.format_exc();print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 API and independent accounting","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,"elapsed_seconds":time.monotonic()-start,
        "pgvnserve_sha256":hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests";output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8");(output/"m4-api-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True);return 0 if status=="PASS" else 1


if __name__=="__main__":raise SystemExit(main())
