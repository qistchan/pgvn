"""M4 capacity, isolation and a short off/on/off overhead characterization.

This measured CLI read workload is an M4 diagnostic. The fixed 60s/300s,
1/8/32-client mixed-load repetitions and performance budgets belong to M6.
"""
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import time
import traceback
import uuid
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME
from test_m3_protocol_smoke import Node,SERVER,PASSWORD,client
from test_m4_smoke import request,ready_status,until
from win_process import resources

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-resources")


def characterize(node,repo,enabled):
    if enabled:
        node.start(); until(lambda:ready_status(node)==200)
    else:
        node.log=(setup.WORK/(node.name+"-off.log")).open("ab")
        node.process=subprocess.Popen([str(SERVER),"--config",str(node.config),"--monitor-disabled"],
            cwd=setup.EMPTY,stdout=node.log,stderr=node.log,creationflags=subprocess.CREATE_NO_WINDOW)
        until(lambda:setup.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn'")=="1")
    try:
        url=node.url("/capacity")
        for _ in range(8): client("svn","info","--xml",url)
        before=resources(node.process.pid); start=time.perf_counter()
        def operation(i):
            at=time.perf_counter(); data=client("svn","info" if i%2 else "list","--xml",url)
            assert data; return (time.perf_counter()-at)*1000
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: latencies=list(pool.map(operation,range(64)))
        elapsed=time.perf_counter()-start; after=resources(node.process.pid)
        return {"monitor_enabled":enabled,"clients":8,"operations":len(latencies),"elapsed_seconds":elapsed,
            "throughput_per_second":len(latencies)/elapsed,"p95_ms":sorted(latencies)[math.ceil(.95*len(latencies))-1],
            "cpu_seconds":after["cpu_seconds"]-before["cpu_seconds"],"working_set_bytes":after["working_set_bytes"],
            "private_bytes":after["private_bytes"]}
    finally: node.stop()


def synthesize():
    # Valid empty repository roots; no physical repo directories and no copied
    # history identifiers. This is a synthetic capacity corpus, not Polarion.
    setup.sql("""BEGIN;
        CREATE TEMP TABLE fixture_ids AS SELECT md5('m4-capacity-'||i)::uuid AS id,i FROM generate_series(1,1000) i;
        INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version)
            SELECT id,id,id,1 FROM fixture_ids;
        INSERT INTO pgfs.directories(repository_id) SELECT id FROM fixture_ids;
        INSERT INTO pgfs.node_revisions(repository_id,node_id,kind,created_rev,created_path,directory_id)
            SELECT d.repository_id,nextval('pgfs.node_ids'),'dir',0,'/',d.directory_id
            FROM pgfs.directories d JOIN fixture_ids f ON f.id=d.repository_id;
        INSERT INTO pgfs.revisions(repository_id,rev,root_node_revision_id)
            SELECT n.repository_id,0,n.node_revision_id FROM pgfs.node_revisions n JOIN fixture_ids f ON f.id=n.repository_id;
        INSERT INTO pgvn.repositories(repository_id,canonical_name,enabled,generation)
            SELECT id,'容量仓库 '||lpad(i::text,4,'0'),false,1 FROM fixture_ids;
        INSERT INTO pgvn.configurations(repository_id,generation,authz_text,published_by)
            SELECT id,1,'[/]'||chr(10)||'writer = rw'||chr(10),'capacity fixture' FROM fixture_ids;
        INSERT INTO pgvn.routes(route_prefix,repository_id) SELECT '/capacity-'||i,id FROM fixture_ids;
        COMMIT;""")
    setup.sql("INSERT INTO pgvn_monitor.nodes SELECT md5('m4-node-'||i)::uuid,'synthetic-'||i,'dav','fixture',1,"
        "clock_timestamp(),clock_timestamp(),jsonb_build_object('padding',repeat('x',3800)) FROM generate_series(1,999) i")


def sampler_timeout(node,token):
    before,_=request(node,token=token)
    tag="pgvn-m4-resource-lock"
    sql="BEGIN; LOCK TABLE pgvn_monitor.nodes IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(30); ROLLBACK;"
    process=subprocess.Popen([str(setup.fixture.pg/"bin/psql.exe"),"-X","-w","-q","-h","127.0.0.1","-p","16432",
        "-U","pgvn_owner","-d",setup.DATABASE,"-c",sql],env=dict(os.environ,PGAPPNAME=tag),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        until(lambda:setup.sql("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING(pid) WHERE a.application_name='"+tag+"' AND l.mode='AccessExclusiveLock' AND l.granted")=="1")
        started=time.monotonic()
        failed=until(lambda:(lambda o:o if o["sample_failures"]>before["sample_failures"] else None)(request(node,token=token)[0]))
        elapsed=time.monotonic()-started
        assert not failed["last_attempt_ok"] and failed["sampler_sql_errors"]>before["sampler_sql_errors"]
        request(node,"/health/live"); request(node,"/health/ready",expected=503)
        client("svnmucc","-m","publication during sampler timeout","mkdir",node.url("/capacity/timeout-safe"))
    finally:
        setup.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='"+setup.DATABASE+"' AND application_name='"+tag+"'")
        process.communicate(timeout=10)
    until(lambda:ready_status(node)==200)
    return {"failure_observed_seconds":elapsed,"live_during_sql_timeout":True,"business_commit_during_sql_timeout":True,"sampler_recovered":True}


def checks():
    setup.setup_database(); setup.fixture.assert_ours()
    live=setup.run(RUNTIME/"bin/monitor_probe.exe",[],role="pgvn_monitor")
    assert json.loads(live.stdout)=={"initial_live":1,"unconfigured_ready":0,"stalled_live":0,"recovered_live":1}
    key=setup.WORK/"resources.key"; setup.admin("key","create",key,"resource-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repo=setup.admin("repo","create","capacity-main","--route","/capacity")
    rules=setup.WORK/"rules.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    token=setup.admin("monitor-token","create","--label","resource test","--expires-in",900)["token"]
    rounds=[characterize(Node("capacity-benchmark-"+str(i),key),repo,enabled) for i,enabled in enumerate((False,True,False))]
    off=(rounds[0]["private_bytes"]+rounds[2]["private_bytes"])/2
    assert rounds[1]["private_bytes"]-off<64*1048576
    node=Node("capacity-live",key); peer=Node("capacity-publish-limit",key)
    try:
        node.start(); until(lambda:ready_status(node)==200)
        timeout_result=sampler_timeout(node,token)
        # Previous benchmark instances would consume two extra node slots.
        setup.sql("DELETE FROM pgvn_monitor.samples WHERE instance_id NOT IN (SELECT instance_id FROM pgvn_monitor.nodes WHERE process_id="+str(node.process.pid)+");"
            "DELETE FROM pgvn_monitor.nodes WHERE process_id<>"+str(node.process.pid))
        baseline=resources(node.process.pid); synthesize()
        until(lambda:request(node,"/api/monitor/v1/repos",token)[0]["truncated"],seconds=20)
        count=0; seen=set(); page,_=request(node,"/api/monitor/v1/repos?limit=200",token)
        while True:
            count+=len(page["items"]); seen.update(r["id"] for r in page["items"])
            if not page["next_cursor"]: break
            page,_=request(node,"/api/monitor/v1/repos?limit=200&cursor="+page["next_cursor"],token)
        assert count==len(seen)==1000
        nodes,_=request(node,"/api/monitor/v1/nodes?limit=200",token); assert len(nodes["items"])==200
        after=resources(node.process.pid); extra=after["private_bytes"]-baseline["private_bytes"]
        assert extra<64*1048576,(baseline,after)
        assert ready_status(node)==200
        # Row capacity failure only drops telemetry; the new process can serve
        # actual SVN commits without altering the business result.
        peer.start(); until(lambda:ready_status(peer)==200)
        dropped=until(lambda:(lambda o:o if o["dropped_telemetry_batches"] else None)(request(peer,token=token)[0]))
        client("svnmucc","-m","publication survives telemetry capacity","mkdir",peer.url("/capacity/after-cap"))
        assert b"after-cap" in client("svn","list",node.url("/capacity"))
        assert setup.sql("SELECT count(*) FROM pgvn_monitor.nodes")=="1000"
        # Cleanup removes at most 2000 rows per publish invocation.
        old=str(uuid.UUID(hashlib.md5(b'm4-node-1').hexdigest()))
        setup.sql(f"UPDATE pgvn_monitor.nodes SET last_seen_at=clock_timestamp()-interval '2 days' WHERE instance_id='{old}';"
            f"INSERT INTO pgvn_monitor.samples SELECT '{old}',clock_timestamp()-interval '2 days'-i*interval '1 second','{{}}'::jsonb FROM generate_series(1,2001) i")
        publish="SELECT pgvn_monitor.publish('"+old+"','old-fixture','dav','test',1,clock_timestamp(),'{}')"
        setup.fixture.sql(publish,database=setup.DATABASE,role="pgvn_service")
        remaining=int(setup.sql("SELECT count(*) FROM pgvn_monitor.samples WHERE sampled_at<clock_timestamp()-interval '24 hours'"))
        assert remaining<=1
        setup.fixture.sql(publish,database=setup.DATABASE,role="pgvn_service")
        assert setup.sql("SELECT count(*) FROM pgvn_monitor.samples WHERE sampled_at<clock_timestamp()-interval '24 hours'")=="0"
        # Capacity token snapshots fail closed, including when the last cache
        # still contains the presented token. They recover after valid cleanup.
        setup.sql("INSERT INTO pgvn.monitor_tokens(token_id,token_hash,label,expires_at) SELECT md5('token-'||i)::uuid,"
            "decode(md5('token-a-'||i)||md5('token-b-'||i),'hex'),'capacity fixture',clock_timestamp()+interval '10 minutes' FROM generate_series(1,1000) i")
        from test_m4_smoke import unauthorized
        until(lambda:unauthorized(node,token),seconds=15)
        setup.sql("DELETE FROM pgvn.monitor_tokens WHERE label='capacity fixture'")
        until(lambda:not unauthorized(node,token),seconds=15)
        until(lambda:request(node,token=token)[0]["last_attempt_ok"],seconds=15)
        request(node,"/health/live")
        setup.admin("verify",repo["id"])
        return {"synthetic_repositories":1000,"total_repositories":1001,"paged_distinct_items":1000,
            "nodes_capped":1000,"private_memory_before":baseline,"private_memory_after":after,"extra_private_bytes":extra,
            "extra_memory_below_64MiB":True,"bounded_24h_cleanup":True,"token_capacity_fails_closed_and_recovers":True,
            "main_loop_stall_and_recovery":True,
            "sampler_statement_timeout":timeout_result,
            "telemetry_capacity_preserves_svn_publication":True,"dropped_batches":dropped["dropped_telemetry_batches"],
            "characterization_rounds":rounds,"performance_scope":"Short CLI read workload; formal mixed-load budgets are measured in M6.","full_verify":True}
    finally: node.stop(); peer.stop()


def main():
    start=time.monotonic()
    try: details=checks(); status="PASS"; error=None
    except Exception: details={}; status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 capacity and measured overhead","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,"elapsed_seconds":time.monotonic()-start,
        "pgvnserve_sha256":hashlib.sha256(SERVER.read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests"; output.mkdir(parents=True,exist_ok=True); data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (output/"m4-resources-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True); return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
