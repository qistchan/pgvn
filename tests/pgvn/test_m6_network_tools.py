"""Test-only original SVN CLI bindings, independently compared with stock FSFS.

--offline runs help/version and preconnection rejection checks without SQL.
--plan prepares a concrete isolated DB/role test plan without executing it.
--execute-plan PLAN requires the named plan and never reuses an existing DB.
No upstream Python assertions are claimed executed by this component.
"""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from fixture import Fixture,ROOT,RUNTIME

RESULTS=ROOT/'artifacts/pgvn/tests'
SELF='tests/pgvn/test_m6_network_tools.py'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
read=lambda p:json.loads(p.read_text(encoding='utf-8'))

def identity():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    manifest=read(manifest_path)
    assert SELF in manifest['sources'] and sha(ROOT/SELF)==manifest['sources'][SELF],'Test component not registered in frozen build'
    tool_path=ROOT/'artifacts/pgvn/build/upstream-network-tools.json'
    tools=read(tool_path)
    assert tools['build_manifest_sha256']==sha(manifest_path),'Test tools belong to another product manifest'
    for name,expected in tools['fixture_sources'].items():assert sha(ROOT/name)==expected,name
    for record in tools['products'].values():assert sha(ROOT/record['path'])==record['sha256']
    for name,expected in tools['runtime_dependencies'].items():assert sha(ROOT/name)==expected,name
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256']
    stock={}
    for name in ('svnadmin','svnlook','svnmucc'):
        relative='bin/'+name+'.exe';expected=manifest['runtime_dependencies'][relative]
        assert sha(RUNTIME/relative)==expected
        stock[name]={'path':(RUNTIME/relative).relative_to(ROOT).as_posix(),'sha256':expected}
    return {'build_manifest_sha256':sha(manifest_path),'tool_manifest_sha256':sha(tool_path),
            'component_source_sha256':sha(ROOT/SELF),'tools':tools['products'],'stock':stock,
            'production_products':{n:r['sha256'] for n,r in manifest['products'].items()}}

def environment(**values):
    # No inherited PostgreSQL password/service configuration is needed here.
    env={k:v for k,v in os.environ.items() if not k.startswith('PGVN_') and k not in ('PGPASSWORD','PGSERVICE','PGSERVICEFILE','PGHOSTADDR')}
    return dict(env,LC_ALL='C',**values)

def invoke(program,args,work,env,input=None,expected=0):
    result=subprocess.run([str(program),*map(str,args)],cwd=work,env=env,input=input,
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=60)
    assert result.returncode==expected,(Path(program).name,args,result.returncode,result.stdout[:1000],result.stderr[:2000])
    return result

def save(report,key):
    RESULTS.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2,ensure_ascii=False)+'\n'
    target=RESULTS/(report['test_id']+'.json');assert not target.exists(),'Do not overwrite test evidence'
    target.write_text(data,encoding='utf-8');(RESULTS/(key+'-latest.json')).write_text(data,encoding='utf-8')
    print(json.dumps({k:report.get(k) for k in ('test_id','status','checks','upstream_python_tests_executed','elapsed_seconds')}),flush=True)

def offline():
    pinned=identity();suffix=uuid.uuid4().hex[:12];test_id='m6-network-tools-offline-'+suffix
    work=RUNTIME/'tests'/test_id;work.mkdir(parents=True);checks=[];start=time.monotonic()
    alias=work/'nonexistent-repository';ledger=work/'commands.jsonl'
    try:
        for name,record in pinned['tools'].items():
            binary=ROOT/record['path']
            version=invoke(binary,['--version','--quiet'],work,environment()).stdout.strip()
            assert version==b'1.14.5',version
            help_text=invoke(binary,['--help'],work,environment()).stdout
            assert name.encode() in help_text and b'Subversion' in help_text
            checks.append(name+' original help/version')
        binary=ROOT/pinned['tools']['svnlook']['path']
        result=subprocess.run([str(binary),'youngest',str(alias)],cwd=work,env=environment(),capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        assert result.returncode and b'Guarded UTF-8 PgVN upstream fixture is required' in result.stderr
        checks.append('missing guard rejected before connection')
        database='pgvn_m6_net_'+suffix
        base={'host':'127.0.0.1','port':'16432','dbname':database,'user':'pgvn_admin','sslmode':'disable'}
        cases=[('host',{'host':'127.0.0.2'}),('port',{'port':'1'}),('database',{'dbname':'postgres'}),
               ('role',{'user':'pgvn_owner'}),('hostaddr',{'hostaddr':'127.0.0.2'}),('service',{'service':'forbidden-test-service'})]
        for label,overrides in cases:
            dsn=' '.join(k+'='+v for k,v in dict(base,**overrides).items())
            env=environment(PGVN_UPSTREAM_DATABASE=database,PGVN_UPSTREAM_ROOT=work.as_posix(),
                            PGVN_UPSTREAM_LEDGER=ledger.as_posix(),PGVN_DSN=dsn)
            result=subprocess.run([str(binary),'youngest',str(alias)],cwd=work,env=env,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
            assert result.returncode and b'Fixture connection options do not match' in result.stderr,(label,result.stderr)
            assert not alias.exists() and not ledger.exists()
            checks.append(label+' rejected before connection')
        assert identity()==pinned
        status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':test_id,'status':status,'component':'M6 upstream tool offline checks','milestone_complete':False,
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
            'upstream_python_tests_executed':0,'database_connections_attempted':0 if status=='PASS' else None,
            'scope':'Help/version and known guard branches only, not a repository or network compatibility result.',
            'inputs':pinned,'elapsed_seconds':time.monotonic()-start}
    save(report,'m6-network-tools-offline');return 0 if status=='PASS' else 1

def make_plan():
    suffix=uuid.uuid4().hex[:12];test_id='m6-network-tools-'+suffix
    work=RUNTIME/'tests'/test_id;work.mkdir(parents=True)
    plan={'format':1,'test_id':test_id,'work':work.relative_to(ROOT).as_posix(),
          'database':'pgvn_m6_net_'+suffix,'owner':'pgvn_migrate_'+suffix,
          'endpoint':'127.0.0.1:16432','cluster_data_directory':str(RUNTIME/'pg-primary'),
          'inputs':identity(),'prepared_at':dt.datetime.now(dt.timezone.utc).isoformat(),
          'actions':['Create one new non-superuser migration role and one isolated synthetic-data database.',
                     'Initialize schema and grant existing PgVN admin/service/monitor roles in this database only.',
                     'Create synthetic stock FSFS history; load/dump/verify and compare original svnlook command outputs through the PgVN ID fixture.',
                     'Retain isolated database/role/data/logs for diagnosis; no network listeners, application accounts or monitor tokens are needed for this CLI component.'],
          'upstream_python_tests_executed':0,'milestone_complete':False}
    p=work/'plan.json';p.write_text(json.dumps(plan,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({'status':'PREPARED_NOT_EXECUTED','plan':str(p),'database':plan['database'],'owner':plan['owner'],'plan_sha256':sha(p),'actions':plan['actions']}))
    return 0

def execute(path):
    path=Path(path).resolve();assert path.is_relative_to((RUNTIME/'tests').resolve()) and path.name=='plan.json'
    plan=read(path);assert plan['format']==1 and plan['inputs']==identity()
    match=re.fullmatch(r'm6-network-tools-([0-9a-f]{12})',plan['test_id']);assert match
    suffix=match[1];work=(ROOT/plan['work']).resolve()
    assert work==path.parent==(RUNTIME/'tests'/plan['test_id']).resolve()
    assert plan['database']=='pgvn_m6_net_'+suffix and plan['owner']=='pgvn_migrate_'+suffix
    assert plan['endpoint']=='127.0.0.1:16432' and plan['cluster_data_directory']==str(RUNTIME/'pg-primary')
    assert not (work/'started.json').exists(),'Never reuse or silently restart a fixture plan'
    (work/'started.json').write_text(json.dumps({'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'plan_sha256':sha(path)}),encoding='utf-8')
    fixture=Fixture();database=plan['database'];owner=plan['owner'];checks=[];start=time.monotonic()
    native_root=work/'native-aliases';native_root.mkdir()
    alias=native_root/'工程 alpha';ledger=native_root/'commands.jsonl'
    dsn=lambda role:f'host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable'
    env=environment(PGVN_UPSTREAM_DATABASE=database,PGVN_UPSTREAM_ROOT=native_root.as_posix(),
                    PGVN_UPSTREAM_LEDGER=ledger.as_posix(),PGVN_DSN=dsn('pgvn_admin'))
    def native(name,*args,**kw):return invoke(ROOT/plan['inputs']['tools'][name]['path'],args,work,env,**kw)
    def stock(name,*args,**kw):return invoke(ROOT/plan['inputs']['stock'][name]['path'],args,work,environment(),**kw)
    def admin(*args,role='pgvn_admin'):
        return json.loads(invoke(RUNTIME/'bin/pgvnadmin.exe',[*args,'--json'],work,environment(PGVN_DSN=dsn(role))).stdout)
    try:
        fixture.assert_ours()
        assert fixture.sql(f"SELECT count(*) FROM pg_database WHERE datname='{database}'",database='postgres')=='0'
        assert fixture.sql(f"SELECT count(*) FROM pg_roles WHERE rolname='{owner}'",database='postgres')=='0'
        fixture.sql(f'CREATE ROLE {owner} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',database='postgres')
        fixture.sql(f"CREATE DATABASE {database} OWNER {owner} ENCODING 'UTF8' TEMPLATE template0",database='postgres')
        admin('db','init',role=owner)
        admin('db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor',role=owner)
        control=work/'fsfs-control';stock('svnadmin','create',control,'--fs-type=fsfs')
        text=work/'text.txt';text.write_bytes(b'first line\nsecond line\n')
        binary=work/'binary.bin';binary.write_bytes(bytes(range(256))*17+b'\x00\xff\r\n')
        url=control.as_uri()
        common=['--non-interactive','--no-auth-cache','--username','jrandom','--config-dir',str(work/'client-config')]
        stock('svnmucc',*common,'-m','initial','mkdir',url+'/A','put',text,url+'/A/readme.txt','put',binary,url+'/A/blob.bin')
        text.write_bytes(b'first line\nchanged second line\n')
        stock('svnmucc',*common,'-m','copy and edit','cp','1',url+'/A',url+'/branch','put',text,url+'/A/readme.txt','propset','test:property','value',url+'/A')
        dumped=stock('svnadmin','dump',control).stdout
        native('svnadmin','create',alias,'--fs-type=pg','--compatible-version=1.14')
        assert not alias.exists();checks.append('ID create without local repository shell')
        native('svnadmin','load',alias,'--force-uuid',input=dumped)
        native('svnadmin','verify',alias);checks.append('original load and verify')
        comparisons=[('youngest',),('uuid',),('tree',),('tree','-r','1'),('changed','-r','2'),
                     ('dirs-changed','-r','2'),('info','-r','2'),('author','-r','2'),('date','-r','2'),
                     ('log','-r','2'),('filesize','A/blob.bin'),('cat','A/blob.bin'),
                     ('propget','test:property','A'),('proplist','--verbose','A'),
                     ('history','A/readme.txt'),('diff','-r','2')]
        for command in comparisons:
            expected=stock('svnlook',command[0],control,*command[1:]).stdout
            actual=native('svnlook',command[0],alias,*command[1:]).stdout
            assert actual==expected,(command,expected[:2000],actual[:2000])
            checks.append('stock FSFS comparison: '+' '.join(command))
        exported=native('svnadmin','dump',alias).stdout
        restored=work/'fsfs-restored';stock('svnadmin','create',restored,'--fs-type=fsfs')
        stock('svnadmin','load',restored,'--force-uuid',input=exported)
        stock('svnadmin','verify',restored)
        for command in comparisons:
            assert stock('svnlook',command[0],restored,*command[1:]).stdout==stock('svnlook',command[0],control,*command[1:]).stdout,command
        checks.append('native dump restored to independently verified stock FSFS')
        events=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        assert events and all(e['process_code_page']==65001 for e in events)
        assert any(e['operation']=='create' for e in events)
        assert not alias.exists()
        assert identity()==plan['inputs'];status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':plan['test_id'],'status':status,'component':'M6 original CLI command bindings','milestone_complete':False,
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'database':database,'work':str(work),
            'checks':checks,'error':error,'inputs':plan['inputs'],'plan_sha256':sha(path),
            'upstream_python_tests_executed':0,'elapsed_seconds':time.monotonic()-start,
            'scope':'Original command bodies with constructor bindings and a synthetic stock-FSFS differential corpus; no claim of SVN/HTTP/HTTPS upstream Python suite acceptance.'}
    if ledger.exists():
        report['native_ledger']={'sha256':sha(ledger)}
        try:report['native_ledger']['events']=[json.loads(l) for l in ledger.read_text(encoding='utf-8').splitlines()]
        except ValueError:report['native_ledger']['parse_error']=traceback.format_exc()
    save(report,'m6-network-tools');return 0 if status=='PASS' else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__);modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--offline',action='store_true');modes.add_argument('--plan',action='store_true');modes.add_argument('--execute-plan')
    args=parser.parse_args()
    if args.offline:return offline()
    if args.plan:return make_plan()
    return execute(args.execute_plan)

if __name__=='__main__':raise SystemExit(main())
