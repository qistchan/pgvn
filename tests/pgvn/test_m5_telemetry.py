"""Match Apache telemetry to access logs and consume it through the read-only API."""
from collections import Counter
import datetime as dt
import hashlib
import json
import time
import traceback
from test_m5_http import Apache,APACHE,run,svn,setup,ROOT
from test_m5_balancer import merge_body
from test_m4_smoke import request,until
from test_m3_protocol_smoke import Node

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-telemetry')
records={}


def snapshots():
    return json.loads(setup.sql("SELECT COALESCE(json_agg(q),'[]'::json)::text FROM "
        "(SELECT instance_id,node_id,component,process_id,metrics,extract(epoch FROM clock_timestamp()-last_seen_at) AS age "
        "FROM pgvn_monitor.nodes WHERE component='dav' ORDER BY node_id,started_at) q"))


def ledger(server):
    rows=(server.work/'access.log').read_text(errors='replace').splitlines()
    counts=Counter(row.split(' ',1)[0] for row in rows)
    errors=Counter(row.split(' ',1)[0] for row in rows if int(row.rsplit(' ',1)[1])>=400)
    return counts,errors


def checks():
    setup.setup_database();route='/svn/telemetry'
    repo=setup.admin('repo','create','telemetry-canonical','--route',route)
    rules=setup.WORK/'authz.conf';rules.write_text('[/]\nwriter = rw\nreader = r\n',encoding='utf-8')
    setup.admin('config','publish',repo['id'],'--expected-generation',1,'--authz',rules)
    token=setup.admin('monitor-token','create','--label','DAV telemetry test','--expires-in',300)['token']
    key=setup.WORK/'monitor.key';setup.admin('key','create',key,'m5-telemetry')
    monitor=Node('observer',key);a=Apache('telemetry-a');b=Apache('telemetry-b')
    try:
        monitor.start();a.start();b.start()
        for server in (a,b):assert server.request('OPTIONS',route)[0]==200
        svn('mkdir',a.url(route)+'/trunk','-m','telemetry r1')
        for _ in range(9):assert a.request('OPTIONS',route)[0]==200
        status,headers,_=a.request('OPTIONS',route,user=None);assert status==401
        denied_id=headers['X-PgVN-Request-Id']
        status,headers,_=a.request('OPTIONS','/svn/missing-route');assert status==404
        assert headers['X-PgVN-Request-Id']!=denied_id
        status,_,body=a.request('POST',route+'/!svn/me',b'( create-txn )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'read-only'},user='reader')
        assert status==403,(status,body)
        status,_,body=a.request('POST',route+'/!svn/me',b'( create-txn )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'cross-worker'})
        assert status==201,(status,body)
        assert b.request('PUT',route+'/!svn/vtxr/cross-worker/data.bin',b'payload\0\xff')[0] in (201,204)
        merge=merge_body(b.url(route+'/!svn/vtxn/cross-worker'))
        for server in (b,a):
            status,_,body=server.request('MERGE',route,merge,{'Content-Type':'text/xml'})
            assert status==200,(status,body)
        assert setup.admin('look','youngest',repo['id'])['youngest']==2
        expected={s.work.name:ledger(s) for s in (a,b)}
        def balanced():
            rows=snapshots()
            if len(rows)!=2:return None
            for server in (a,b):
                node=next((r for r in rows if r['process_id']==server.process.pid),None)
                if not node or node['metrics']['requests']!=sum(expected[server.work.name][0].values()):return None
            return rows
        rows=until(balanced,20)
        for server in (a,b):
            node=next(r for r in rows if r['process_id']==server.process.pid);m=node['metrics'];counts,errors=expected[server.work.name]
            assert {row['method']:row['requests'] for row in m['methods'] if row['requests']}==dict(counts)
            assert {row['method']:row['errors'] for row in m['methods'] if row['errors']}==dict(errors)
            assert m['request_errors']==sum(errors.values()) and m['requests_active']==0
            assert m['request_latency_cumulative'][-1]==m['requests']
            assert all(x<=y for x,y in zip(m['request_latency_cumulative'],m['request_latency_cumulative'][1:]))
            assert m['sql_calls']>0 and m['connections']>=1 and m['ready'] is True
            assert m['working_set_bytes']>0 and m['private_bytes']>0 and len(m['recent_errors'])<=5
            assert len(json.dumps(m).encode())<=4096
        assert sum(row['metrics']['published_revisions'] for row in rows)==2
        assert sum(row['metrics']['receipt_confirmations'] for row in rows)==1
        assert any(error['request_id']==denied_id for row in rows for error in row['metrics']['recent_errors'])
        ids={row['instance_id'] for row in rows}
        def api_rows():
            payload,_=request(monitor,'/api/monitor/v1/nodes?limit=200',token=token)
            return payload if ids<={row['instance_id'] for row in payload['items']} else None
        payload=until(api_rows,20)
        assert all(row['component']=='dav' for row in payload['items'] if row['instance_id'] in ids)
        signature='pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb)'
        try:
            setup.sql('REVOKE EXECUTE ON FUNCTION '+signature+' FROM pgvn_service')
            assert setup.sql("SELECT has_function_privilege('pgvn_service','"+signature+"','EXECUTE')")=='f'
            time.sleep(5.5)
            svn('mkdir',a.url(route)+'/monitor-failure','-m','telemetry failure does not block r3')
            assert b'entry' in svn('info','--xml',b.url(route)+'/monitor-failure')
        finally:setup.sql('GRANT EXECUTE ON FUNCTION '+signature+' TO pgvn_service')
        recovered=until(lambda:next((r for r in snapshots() if r['metrics']['telemetry_failures']>0),None),15)
        assert setup.admin('look','youngest',repo['id'])['youngest']==3
        records['telemetry_permission_failure_does_not_block_real_commit']=True
        records['telemetry_recovers_after_privilege_restore']=True
        stopped_id=next(row['instance_id'] for row in rows if row['process_id']==a.process.pid)
        a.close()
        until(lambda:any(r['instance_id']==stopped_id and float(r['age'])>15 for r in snapshots()),20)
        a.start();assert a.request('OPTIONS',route)[0]==200
        newer=until(lambda:next((r for r in snapshots() if r['process_id']==a.process.pid and r['instance_id']!=stopped_id),None),20)
        assert newer['node_id']=='dav-telemetry-a'
        setup.admin('verify',repo['id'])
        records.update({'access_log_method_and_error_counts_exact':True,'active_zero_and_histogram_balance':True,
            'new_publications_two_receipt_one':True,'response_request_id_in_bounded_recent_errors':True,
            'worker_memory_and_connections':True,'restricted_monitor_api_observes_dav_workers':True,
            'lost_worker_heartbeat_ages_without_false_stop':True,'restart_has_new_instance':True,
            'workers':rows,'full_verify':True})
        return records
    finally:a.close();b.close();monitor.stop()


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 Apache worker telemetry','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,'elapsed_seconds':time.monotonic()-start,
        'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest() for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m5-telemetry-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='details'}),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
