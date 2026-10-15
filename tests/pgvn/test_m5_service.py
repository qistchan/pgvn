"""Own uniquely named Apache LocalService; never controls installed services."""
import datetime as dt
import hashlib
import json
import re
import subprocess
import time
import traceback
from test_m5_http import Apache,APACHE,run,svn,setup,ROOT
from test_m4_smoke import until

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-dav-service')
NAME='PgVNDavM5_'+TEST_ID.rsplit('-',1)[1]
records={}


def sc(*args,expected=0):
    result=subprocess.run(['sc.exe',*map(str,args)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    assert result.returncode==expected,(args,result.returncode,result.stdout,result.stderr)
    return result.stdout.decode(errors='replace')


def state():
    result=subprocess.run(['sc.exe','query',NAME],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
    if result.returncode==1060:return 0
    assert result.returncode==0,result.stdout
    match=re.search(r'STATE\s*:\s*(\d+)',result.stdout.decode(errors='replace'));assert match,result.stdout
    return int(match[1])


def checks():
    setup.setup_database();route='/svn/service'
    repo=setup.admin('repo','create','dav-service','--route',route)
    rules=setup.WORK/'rules.conf';rules.write_text('[/]\nwriter = rw\n',encoding='utf-8')
    setup.admin('config','publish',repo['id'],'--expected-generation',1,'--authz',rules)
    server=Apache('service');installed=False;cleanup=False
    # Scope the writable log/config permission to this fixture directory.
    run('icacls.exe',server.work,'/grant','*S-1-5-19:(OI)(CI)(M)','/T','/Q')
    try:
        assert state()==0,'Unique fixture service unexpectedly exists'
        run(APACHE/'bin/pgvnhttpd.exe','-t','-f',server.config)
        run(APACHE/'bin/pgvnhttpd.exe','-k','install','-n',NAME,'-f',server.config)
        installed=True
        sc('config',NAME,'obj=','NT AUTHORITY\LocalService','start=','demand')
        assert 'LocalService' in sc('qc',NAME)
        sc('start',NAME);until(lambda:state()==4,20)
        def listening():
            try:return server.request('OPTIONS',route)[0]==200
            except OSError:return False
        until(listening,20)
        svn('mkdir',server.url(route)+'/trunk','-m','LocalService DAV commit')
        def ready_worker():
            value=setup.sql("SELECT row_to_json(q)::text FROM (SELECT instance_id,process_id,metrics "
                "FROM pgvn_monitor.nodes WHERE component='dav' AND metrics->>'ready'='true' "
                "AND metrics->>'published_revisions'='1' ORDER BY started_at DESC LIMIT 1) q")
            return json.loads(value) if value else None
        worker=until(ready_worker,15)
        parent=int(re.search(r'PID\s*:\s*(\d+)',sc('queryex',NAME))[1]);assert worker['process_id']!=parent
        start=time.monotonic();sc('stop',NAME);until(lambda:state()==1,25);elapsed=time.monotonic()-start
        stopped=json.loads(setup.sql("SELECT metrics::text FROM pgvn_monitor.nodes WHERE instance_id='"+worker['instance_id']+"'"))
        assert stopped['stopped'] is True and stopped['ready'] is False,stopped
        assert stopped['requests_active']==0 and stopped['connections']==0,stopped
        setup.admin('verify',repo['id'])
        records.update({'account':'NT AUTHORITY\\LocalService','real_dav_commit':True,'parent_pid':parent,
            'worker_pid':worker['process_id'],'worker_instance':worker['instance_id'],'telemetry_registered_in_worker_only':True,
            'normal_stop_publishes_stopped_and_drains_connections':True,'stop_seconds':elapsed,'full_verify':True})
    finally:
        if installed or state()!=0:
            if state() not in (0,1):
                result=subprocess.run(['sc.exe','stop',NAME],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
                until(lambda:state() in (0,1),30)
            if state()!=0:run(APACHE/'bin/pgvnhttpd.exe','-k','uninstall','-n',NAME)
            until(lambda:state()==0,10);cleanup=True
        records['unique_service_removed']=cleanup
    return records


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 native Apache service lifecycle','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,'elapsed_seconds':time.monotonic()-start,
        'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest() for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m5-dav-service-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
