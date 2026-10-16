"""Local FSFS/WC checks for private upstream helpers; no network/SQL fixture."""
from pathlib import Path
import datetime as dt
import json
import sqlite3
import sys
import time
import traceback
import uuid
from fixture import ROOT,RUNTIME
from test_m6_network_tools import sha,read,environment,invoke,save

SELF='tests/pgvn/test_m6_upstream_helpers.py'

def identity():
    path=ROOT/'artifacts/pgvn/build/manifest.json';manifest=read(path)
    assert sha(ROOT/SELF)==manifest['sources'][SELF],'Unregistered helper check'
    build=ROOT/'artifacts/pgvn/build/upstream-helpers.json';helpers=read(build)
    assert helpers['build_manifest_sha256']==sha(path)
    assert helpers['builder_sha256']==sha(ROOT/'scripts/pgvn/build_upstream_helpers.py')
    assert helpers['upstream_network_tools_manifest_sha256']==sha(ROOT/'artifacts/pgvn/build/upstream-network-tools.json')
    for record in helpers['products'].values():assert sha(ROOT/record['path'])==record['sha256']
    for name,expected in helpers['runtime_dependencies'].items():assert sha(ROOT/name)==expected
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256']
    stock={}
    for name in ('svn','svnadmin','svnlook','svnmucc'):
        relative='bin/'+name+'.exe';assert sha(RUNTIME/relative)==manifest['runtime_dependencies'][relative]
        stock[name]=str(RUNTIME/relative)
    return {'manifest_sha256':sha(path),'helpers_manifest_sha256':sha(build),
            'products':helpers['products'],'stock':stock,'source_sha256':sha(ROOT/SELF)}

def main():
    pinned=identity();test_id='m6-upstream-helpers-'+uuid.uuid4().hex[:12]
    work=RUNTIME/'tests'/test_id;work.mkdir(parents=True);checks=[];start=time.monotonic()
    env=environment()
    def stock(name,*args,**kw):return invoke(pinned['stock'][name],args,work,env,**kw)
    def helper(name,*args,**kw):return invoke(ROOT/pinned['products'][name]['path'],args,work,env,**kw)
    try:
        for name in pinned['products']:
            result=helper(name,expected=1)
            assert b'usage:' in result.stderr.lower(),(name,result.stderr)
            checks.append(name+' real usage branch')
        control=work/'fsfs';stock('svnadmin','create',control,'--fs-type=fsfs')
        content=work/'file.txt';content.write_bytes(b'helper runtime verification\n')
        url=control.as_uri();config=work/'client-config'
        common=['--non-interactive','--no-auth-cache','--username','jrandom','--config-dir',config]
        stock('svnmucc',*common,'-m','fixture','mkdir',url+'/A','put',content,url+'/A/file.txt')
        wc=work/'wc';stock('svn','checkout',url,wc,*common)
        for mode,key in (('--entries','entries'),('--tree-dump','dirs')):
            result=helper('entries-dump',mode,wc/'A');assert not result.stderr,result.stderr
            namespace={'entries':{},'dirs':{}};exec(result.stdout.decode('utf-8'),namespace,namespace)
            entries=namespace[key]
            if mode=='--tree-dump':entries=next(v for k,v in entries.items() if k.replace('\\','/').endswith('/A'))
            assert entries['file.txt'].revision==1 and entries[''].revision==1
            checks.append('entries-dump '+mode+' reads stock working copy')
        dirs=helper('entries-dump','--subdirs',wc).stdout.decode('utf-8').splitlines()
        assert {Path(p).resolve() for p in dirs}=={wc.resolve(),(wc/'A').resolve()}
        checks.append('entries-dump subdirectories')
        def scalar(sql):
            with sqlite3.connect((wc/'.svn/wc.db').as_uri()+'?mode=ro',uri=True) as db:return db.execute(sql).fetchone()[0]
        helper('wc-lock-tester','-1',wc/'A')
        assert scalar('SELECT count(*) FROM WC_LOCK')==1
        stock('svn','cleanup',wc,*common)
        assert scalar('SELECT count(*) FROM WC_LOCK')==0
        checks.append('WC helper creates real write lock and stock cleanup releases it')
        helper('wc-incomplete-tester',wc/'A','1')
        assert scalar("SELECT presence FROM NODES WHERE local_relpath='A' AND op_depth=0")=='incomplete'
        stock('svn','update',wc,*common)
        assert scalar("SELECT presence FROM NODES WHERE local_relpath='A' AND op_depth=0")=='normal'
        checks.append('WC incomplete marker and stock update repair')
        (control/'hooks/pre-revprop-change.bat').write_bytes(b'@exit /b 0\r\n')
        helper('atomic-ra-revprop-change',url,'1','test:atomic','(value one)','0',config)
        helper('atomic-ra-revprop-change',url,'1','test:atomic','(old_value_p one value two)','0',config)
        helper('atomic-ra-revprop-change',url,'1','test:atomic','(old_value_p one value three)','1',config)
        assert stock('svnlook','propget','--revprop','-r','1',control,'test:atomic').stdout.strip()==b'two'
        checks.append('atomic revision property helper success and failed compare leave expected value')
        helper('lock-helper-fsfs',control,'A/file.txt','jrandom','60')
        assert b'jrandom' in stock('svnadmin','lslocks',control).stdout
        checks.append('original FSFS lock helper creates real expiring lock')
        alias=work/'no-native-repo'
        rejected=helper('lock-helper',alias,'A/file.txt','jrandom','60',expected=1)
        assert b'Guarded UTF-8 PgVN upstream fixture is required' in rejected.stderr
        assert not alias.exists();checks.append('native lock helper rejects missing database guard')
        assert identity()==pinned;status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':test_id,'status':status,'component':'M6 private upstream helper local checks',
            'milestone_complete':False,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'checks':checks,'error':error,'inputs':pinned,'work':str(work),
            'upstream_python_tests_executed':0,'database_connections_attempted':0 if status=='PASS' else None,
            'network_listeners_started':0,'elapsed_seconds':time.monotonic()-start,
            'scope':'Original C helper bodies over a local stock FSFS/working copy; not upstream Python or network acceptance.'}
    save(report,'m6-upstream-helpers');return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
