"""HTTP negative authorization for hidden history, copies and fresh policy."""
import datetime as dt
import hashlib
import json
import time
import traceback
from test_m5_http import Apache,APACHE,SVN,PASSWORD,run,svn,setup,ROOT,RUNTIME
from test_m5_balancer import merge_body

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-authz')
records={}


def checks():
    setup.setup_database();a=setup.admin('repo','create','alpha','--route','/svn/a')
    b=setup.admin('repo','create','beta','--route','/svn/b')
    rules=setup.WORK/'alpha.conf';rules.write_text('[alpha:/]\nwriter = rw\nother = rw\nreader = r\n'
        '[alpha:/secret]\nreader =\nother =\n',encoding='utf-8')
    other=setup.WORK/'beta.conf';other.write_text('[beta:/]\nwriter = rw\nother =\nreader = r\n',encoding='utf-8')
    setup.admin('config','publish',a['id'],'--expected-generation',1,'--authz',rules)
    setup.admin('config','publish',b['id'],'--expected-generation',1,'--authz',other)
    server=Apache('authz')
    server.config.write_text(server.config.read_text(encoding='utf-8').replace('LogLevel warn','LogLevel debug'),encoding='utf-8')
    try:
        text=server.config.read_text(encoding='utf-8')
        for index,directive in enumerate(('PgVNPathAuthz Off','AuthzPgVNAuthoritative Off','PgVNPath C:/not-a-repository')):
            invalid=server.work/('invalid-'+str(index)+'.conf');invalid.write_text(text.replace('DAV pgvn\n','DAV pgvn\n'+directive+'\n'),encoding='utf-8')
            run(APACHE/'bin/httpd.exe','-t','-f',invalid,expected=1)
        missing=server.work/'missing-authz.conf'
        missing.write_text(text.replace('LoadModule authz_pgvn_module modules/mod_authz_pgvn.so\n',''),encoding='utf-8')
        run(APACHE/'bin/httpd.exe','-X','-f',missing,expected=1,timeout=10)
        server.start();url=server.url('/svn/a');target=server.url('/svn/b');data=setup.WORK/'content.bin';data.write_bytes(b'private\0\xff')
        run(RUNTIME/'bin/svnmucc.exe','--non-interactive','--no-auth-cache','--username','writer','--password',PASSWORD,'-m','seed',
            'mkdir',url+'/public','mkdir',url+'/public/sub','mkdir',url+'/secret','put',data,url+'/public/file.bin','put',data,url+'/secret/hidden.bin')
        assert svn('cat',url+'/public/file.bin',user='reader')==data.read_bytes()
        svn('cat',url+'/secret/hidden.bin',user='reader',expected=1)
        listing=svn('list','-R',url,user='reader');assert b'secret' not in listing and b'hidden.bin' not in listing
        svn('cat','-r','1',url+'/secret/hidden.bin',user='reader',expected=1)
        svn('copy',url+'/secret/hidden.bin',url+'/public/export.bin','-m','copy r2')
        log=svn('log','--xml','-v',url,user='reader')
        assert b'copyfrom-path="/secret' not in log and b'/secret/hidden.bin' not in log,log
        # A COPY source and destination load different repository policies.
        status,_,body=server.request('COPY','/svn/a/public',headers={'Destination':target+'/new','Depth':'infinity'},user='other')
        assert status==403,(status,body)
        assert setup.admin('look','youngest',b['id'])['youngest']==0
        status,_,body=server.request('POST','/svn/b/!svn/me',b'( create-txn )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'cross-repository'})
        assert status==201,(status,body)
        status,_,body=server.request('COPY','/svn/a/!svn/rvr/2/public',headers={
            'Destination':target+'/!svn/vtxr/cross-repository/new','Depth':'infinity'})
        assert status==400 and b'different repositories' in body,(status,body)
        assert server.request('DELETE','/svn/b/!svn/vtxn/cross-repository')[0]==204
        assert setup.admin('look','youngest',b['id'])['youngest']==0
        assert setup.run(setup.PROBE,['policy',a['id'],'other','/','read-recursive'],role='pgvn_service').stdout.strip()==b'denied'
        assert setup.run(setup.PROBE,['policy',a['id'],'other','/public','read-recursive'],role='pgvn_service').stdout.strip()==b'allowed'
        # Recursive source authz must deny the hidden subtree even when the
        # destination is writable and the transaction belongs to this user.
        status,_,body=server.request('POST','/svn/a/!svn/me',b'( create-txn )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'copy-denial'},user='other')
        assert status==201,(status,body)
        status,_,body=server.request('COPY','/svn/a/!svn/rvr/2',headers={
            'Destination':url+'/!svn/vtxr/copy-denial/copy-all','Depth':'infinity'},user='other')
        assert status==403,(status,body)
        assert server.request('DELETE','/svn/a/!svn/vtxn/copy-denial',user='other')[0]==204
        assert setup.admin('look','youngest',a['id'])['youngest']==2
        # Inherited property and mergeinfo entry points retain path filtering.
        wc=setup.WORK/'wc';svn('checkout',url+'/public',wc)
        svn('propset','test:inherited','public-value',wc);svn('commit',wc,'-m','public prop r3')
        assert b'public-value' in svn('propget','--show-inherited-props','test:inherited',url+'/public/sub',user='reader')
        svn('propget','--show-inherited-props','test:inherited',url+'/secret',user='reader',expected=1)
        svn('mergeinfo','--show-revs','merged',url+'/public',url+'/public/sub',user='reader')
        svn('mergeinfo','--show-revs','merged',url+'/public',url+'/secret',user='reader',expected=1)
        # Existing HTTP connection and active transaction cannot pin old authz.
        import http.client,base64
        connection=http.client.HTTPConnection('127.0.0.1',server.port,timeout=10)
        auth={'Authorization':'Basic '+base64.b64encode(('writer:'+PASSWORD).encode()).decode()}
        try:
            connection.request('GET','/svn/a/public/file.bin',headers=auth);response=connection.getresponse();assert response.status==200;response.read()
            status,_,body=server.request('POST','/svn/a/!svn/me',b'( create-txn )',
                {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'revoked'})
            assert status==201,(status,body)
            assert server.request('PUT','/svn/a/!svn/vtxr/revoked/pending.bin',b'must not publish')[0] in (201,204)
            deny=setup.WORK/'deny.conf';deny.write_text('[/]\nreader = r\n',encoding='utf-8')
            setup.admin('config','publish',a['id'],'--expected-generation',2,'--authz',deny)
            connection.request('GET','/svn/a/public/file.bin',headers=auth);response=connection.getresponse();assert response.status==403;response.read()
            status,_,body=server.request('MERGE','/svn/a',merge_body(url+'/!svn/vtxn/revoked'),{'Content-Type':'text/xml'})
            assert status==403,(status,body)
            assert setup.admin('look','youngest',a['id'])['youngest']==3
        finally:connection.close()
        setup.admin('config','publish',a['id'],'--expected-generation',3,'--authz',rules)
        try:
            setup.fixture.sql('ALTER DATABASE '+setup.DATABASE+' ALLOW_CONNECTIONS false',database='postgres')
            setup.fixture.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='"+setup.DATABASE+"'",database='postgres')
            status,headers,body=server.request('GET','/svn/a/public/file.bin')
            assert status==503 and headers.get('X-PgVN-Request-Id'),(status,body,headers)
        finally:setup.fixture.sql('ALTER DATABASE '+setup.DATABASE+' ALLOW_CONNECTIONS true',database='postgres')
        from test_m4_smoke import until
        until(lambda:server.request('GET','/svn/a/public/file.bin')[0]==200,10)
        for repo in (a,b):setup.admin('verify',repo['id'])
        records.update({'hidden_read_list_and_history_denied':True,'copyfrom_hidden_path_filtered_in_log':True,
            'cross_repository_destination_policy_checked':True,'recursive_copy_source_denied':True,
            'inherited_props_and_mergeinfo_path_checks':True,'keepalive_policy_revocation':True,
            'staged_transaction_cannot_publish_after_revocation':True,'full_verify':True})
        records.update({'unsafe_configuration_rejected':True,'missing_authz_module_refused_at_startup':True,
            'database_failure_is_503_and_recovers':True,'authorized_cross_repository_copy_rejected_without_path_io':True})
        return records
    finally:server.close()


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 HTTP authorization','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,'elapsed_seconds':time.monotonic()-start,
        'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest() for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m5-authz-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
