"""Isolated original upstream assertions over SVN/HTTP/HTTPS and stock FSFS.

--plan prepares a single-use frozen-input run. --execute-plan executes only
that run. Every transport uses only private loopback service processes.
"""
from pathlib import Path
from collections import Counter
import argparse
import ctypes
from ctypes import wintypes as wt
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import uuid
from fixture import Fixture,ROOT,RUNTIME
from test_m6_network_tools import sha,read,environment,invoke,save,identity as tools_identity
from test_m6_upstream_helpers import identity as helpers_identity

sys.path.insert(0,str(ROOT/'scripts/pgvn'))
from catalog_upstream_network import read_lock,verify_inputs,materialize_snapshot,suite_catalog

OWN=('tests/pgvn/test_m6_network.py','tests/pgvn/upstream_network_entry.py','tests/pgvn/upstream_hook_bridge.py',
     'tests/pgvn/upstream_network_policy.py','tests/pgvn/upstream_network_import.py','tests/pgvn/upstream_hook_output.py',
     'tests/pgvn/upstream_network_copy.py','tests/pgvn/upstream_network_log.py')

def identity(protocol='svn'):
    assert protocol in ('svn','http','https')
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';manifest=read(manifest_path)
    for name in OWN:assert sha(ROOT/name)==manifest['sources'][name],'Unregistered network input: '+name
    stock=RUNTIME/'bin/svnserve.exe';assert sha(stock)==manifest['runtime_dependencies']['bin/svnserve.exe']
    result={'manifest_sha256':sha(manifest_path),'sources':{n:sha(ROOT/n) for n in OWN},
            'tools':tools_identity(),'helpers':helpers_identity(),
            'stock_svnserve_sha256':sha(stock),'python_sha256':sha(Path(sys.executable))}
    if protocol!='svn':
        import upstream_network_http as http
        result['http']=http.identity(manifest)
    return result

def upstream_inputs():
    manifest=read(ROOT/'artifacts/pgvn/build/manifest.json');source=Path(manifest['inputs']['subversion'])
    lock=read_lock();archive=source.parent/lock['ARCHIVE_BASENAME'];verify_inputs(source,archive,lock)
    return source,archive,lock

def numbers(value):
    result=[]
    for token in value.split(','):
        if ':' in token:
            first,last=map(int,token.split(':'));assert first<=last
            result.extend(range(first,last+1))
        else:result.append(int(token))
    assert result and len(set(result))==len(result) and min(result)>0
    return result

def plan(args):
    assert re.fullmatch('[a-z][a-z0-9_]*_tests',args.suite)
    source,archive,lock=upstream_inputs();suite=suite_catalog(source,source/'subversion/tests/cmdline'/(args.suite+'.py'))
    selected=numbers(args.cases);assert max(selected)<=suite['test_count']
    frozen=identity(args.protocol)
    suffix=uuid.uuid4().hex[:12];test_id='m6-network-'+suffix
    work=RUNTIME/'tests'/test_id;work.mkdir(parents=True)
    value={'format':1,'test_id':test_id,'work':work.relative_to(ROOT).as_posix(),
           'database':'pgvn_m6_net_'+suffix,'owner':'pgvn_migrate_'+suffix,
           'endpoint':'127.0.0.1:16432','cluster_data_directory':str(RUNTIME/'pg-primary'),
           'inputs':frozen,'suite':args.suite,'cases':selected,'protocol':args.protocol,
           'upstream_cases':[case for case in suite['tests'] if case['id'] in selected],
           'prepared_at':dt.datetime.now(dt.timezone.utc).isoformat(),'milestone_complete':False,
           'actions':['Create one new pgvn_m6_net_ database and non-superuser migration owner in the dedicated cluster.',
                      'Create standard SVN fixture accounts and a key; no monitor tokens.',
                      'Run unchanged selected Python assertions serially through stock FSFS and native PgVN over '+args.protocol+' on loopback ephemeral ports.',
                      'HTTPS uses a private two-day CA and verifies hostname/chain in private client configuration; no machine trust-store changes.',
                      'Bind native admin/configuration/hook/lifecycle fixtures; retain all original assertions and decorators.',
                      'Stop owned servers and child jobs; retain database, roles, synthetic data and logs for diagnosis.']}
    path=work/'plan.json';path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'PREPARED_NOT_EXECUTED','plan':str(path),'database':value['database'],
                      'suite':args.suite,'cases':selected,'protocol':args.protocol,'plan_sha256':sha(path)}))
    return 0

class BasicLimits(ctypes.Structure):
    _fields_=[('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('Flags',wt.DWORD),
              ('MinimumWS',ctypes.c_size_t),('MaximumWS',ctypes.c_size_t),('ActiveLimit',wt.DWORD),
              ('Affinity',ctypes.c_size_t),('Priority',wt.DWORD),('Scheduling',wt.DWORD)]
class IO(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
class ExtendedLimits(ctypes.Structure):
    _fields_=[('Basic',BasicLimits),('IO',IO),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),
              ('PeakProcessMemory',ctypes.c_size_t),('PeakJobMemory',ctypes.c_size_t)]
class Accounting(ctypes.Structure):
    _fields_=[(n,ctypes.c_int64) for n in ('User','Kernel','PeriodUser','PeriodKernel')]+[
        (n,wt.DWORD) for n in ('PageFaults','Total','Active','Terminated')]

class OwnedProcess:
    def __init__(self,command,work,env,log):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True);api=self.api
        api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wt.LPCWSTR];api.CreateJobObjectW.restype=wt.HANDLE
        api.SetInformationJobObject.argtypes=[wt.HANDLE,ctypes.c_int,ctypes.c_void_p,wt.DWORD];api.SetInformationJobObject.restype=wt.BOOL
        api.AssignProcessToJobObject.argtypes=[wt.HANDLE,wt.HANDLE];api.AssignProcessToJobObject.restype=wt.BOOL
        api.TerminateJobObject.argtypes=[wt.HANDLE,wt.UINT];api.TerminateJobObject.restype=wt.BOOL
        api.QueryInformationJobObject.argtypes=[wt.HANDLE,ctypes.c_int,ctypes.c_void_p,wt.DWORD,ctypes.c_void_p];api.QueryInformationJobObject.restype=wt.BOOL
        api.CloseHandle.argtypes=[wt.HANDLE];api.CloseHandle.restype=wt.BOOL
        self.job=api.CreateJobObjectW(None,None);assert self.job,ctypes.get_last_error()
        limits=ExtendedLimits();limits.Basic.Flags=0x2000
        if not api.SetInformationJobObject(self.job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error=ctypes.get_last_error();api.CloseHandle(self.job);raise OSError(error,'Cannot guard owned process job')
        self.log=log.open('ab',buffering=0);self.process=None;self.closed=False
        try:
            self.process=subprocess.Popen(list(map(str,command)),cwd=work,env=env,stdout=self.log,stderr=self.log,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            assert api.AssignProcessToJobObject(self.job,wt.HANDLE(int(self.process._handle))),ctypes.get_last_error()
        except BaseException:
            if self.process:self.process.terminate();self.process.wait(timeout=15)
            api.CloseHandle(self.job);self.log.close();raise
    def active(self):
        value=Accounting();assert self.api.QueryInformationJobObject(self.job,1,ctypes.byref(value),ctypes.sizeof(value),None)
        return value.Active
    def stop(self):
        if self.closed:return self.cleanup
        if self.active():assert self.api.TerminateJobObject(self.job,1),ctypes.get_last_error()
        self.process.wait(timeout=15);deadline=time.monotonic()+10
        while self.active() and time.monotonic()<deadline:time.sleep(.05)
        remaining=self.active();self.cleanup={'pid':self.process.pid,'exit_code':self.process.returncode,'job_active_processes':remaining}
        self.api.CloseHandle(self.job);self.log.close();self.closed=True
        assert remaining==0,self.cleanup
        return self.cleanup

def port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def listen(process,number):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        assert process.process.poll() is None,'Owned server exited during startup'
        try:
            with socket.create_connection(('127.0.0.1',number),timeout=.3):pass
            assert process.process.poll() is None
            return
        except OSError:time.sleep(.1)
    raise AssertionError('Owned server did not listen')

def execute(path):
    path=Path(path).resolve();assert path.name=='plan.json' and path.is_relative_to((RUNTIME/'tests').resolve())
    value=read(path);protocol=value['protocol']
    assert value['format']==1 and protocol in ('svn','http','https') and value['inputs']==identity(protocol)
    match=re.fullmatch('m6-network-([0-9a-f]{12})',value['test_id']);assert match
    suffix=match[1];work=(ROOT/value['work']).resolve()
    assert work==path.parent==(RUNTIME/'tests'/value['test_id']).resolve()
    assert value['database']=='pgvn_m6_net_'+suffix and value['owner']=='pgvn_migrate_'+suffix
    assert value['endpoint']=='127.0.0.1:16432' and value['cluster_data_directory']==str(RUNTIME/'pg-primary')
    assert re.fullmatch('[a-z][a-z0-9_]*_tests',value['suite']) and value['cases']==numbers(','.join(map(str,value['cases'])))
    assert not (work/'started.json').exists(),'Never reuse or silently restart an executed fixture plan'
    (work/'started.json').write_text(json.dumps({'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'plan_sha256':sha(path)}),encoding='utf-8')
    database=value['database'];owner=value['owner'];fixture=Fixture();start=time.monotonic()
    processes=[];cleanup=[];rounds=[];snapshot=None;error=None;status='FAIL';tls=None
    dsn=lambda role:f'host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable'
    def admin(*args,role='pgvn_admin',input=None):
        result=invoke(RUNTIME/'bin/pgvnadmin.exe',[*args,'--json'],work,environment(PGVN_DSN=dsn(role)),input=input)
        return json.loads(result.stdout)
    def native_config(area,name,number):
        path=area/name;path.write_text('[pgvn]\nconnection = '+dsn('pgvn_service')+
            '\nmonitor-connection = '+dsn('pgvn_monitor')+'\ncredential-key-file = '+key.as_posix()+
            '\nidentity-domain = '+domain+'\nhook-root = '+Path(sys.executable).parent.as_posix()+
            '\nlisten-host = 127.0.0.1\nlisten-port = '+str(number)+'\n',encoding='utf-8')
        return path
    try:
        fixture.assert_ours();source,archive,lock=upstream_inputs()
        suite=suite_catalog(source,source/'subversion/tests/cmdline'/(value['suite']+'.py'))
        assert value['upstream_cases']==[c for c in suite['tests'] if c['id'] in value['cases']]
        created=materialize_snapshot(archive,lock,value['test_id'])
        snapshot={k:v for k,v in created.items() if k not in ('upstream_modules_imported','bytecode_files')}
        snapshot['creation']={'upstream_modules_imported':False,'bytecode_files':0}
        assert fixture.sql(f"SELECT count(*) FROM pg_database WHERE datname='{database}'",database='postgres')=='0'
        assert fixture.sql(f"SELECT count(*) FROM pg_roles WHERE rolname='{owner}'",database='postgres')=='0'
        fixture.sql(f'CREATE ROLE {owner} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',database='postgres')
        fixture.sql(f"CREATE DATABASE {database} OWNER {owner} ENCODING 'UTF8' TEMPLATE template0",database='postgres')
        admin('db','init',role=owner)
        admin('db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor',role=owner)
        key=work/'credentials.key';admin('key','create',key,'upstream-'+suffix)
        domain='upstream-'+suffix
        for user in ('jrandom','jconstant'):
            admin('user','set',domain,user,'--key-file',key,input=b'rayjandom')
        if protocol=='https':
            import upstream_network_http as http
            temporary=work/'tls-tmp';temporary.mkdir()
            tls=http.tls_material(work,environment(TEMP=str(temporary),TMP=str(temporary)))
        for backend in ('fsfs','pgvn'):
            area=work/backend;area.mkdir();temporary=area/'tmp';temporary.mkdir()
            number=port();env=environment(TEMP=str(temporary),TMP=str(temporary))
            server_inputs=None;tls_check=None;local_server=None;local_inputs=None
            if protocol!='svn':
                import upstream_network_http as http
                server_inputs=http.configure(area,backend,number,database,domain,
                    read(ROOT/'artifacts/pgvn/build/manifest.json'),env,tls)
                command=server_inputs['command']
            elif backend=='fsfs':
                command=[RUNTIME/'bin/svnserve.exe','--daemon','--foreground','--root',area,'--listen-host','127.0.0.1','--listen-port',number]
            else:
                config=native_config(area,'server.ini',number)
                command=[RUNTIME/'bin/pgvnserve.exe','--config',config,'--monitor-disabled']
            server=OwnedProcess(command,area,env,area/'server.log');processes.append(server);listen(server,number)
            if tls:tls_check=http.verify_tls(number,tls)
            if backend=='pgvn' and protocol!='svn':
                local_port=port();local_config=native_config(area,'local-fixture-svn.ini',local_port)
                local_server=OwnedProcess([RUNTIME/'bin/pgvnserve.exe','--config',local_config,'--monitor-disabled'],
                                          area,env,area/'local-fixture-svn.log')
                processes.append(local_server);listen(local_server,local_port)
                local_inputs={'url':f'svn://127.0.0.1:{local_port}','config_sha256':sha(local_config),
                              'purpose':'Only legacy file:// svnmucc setup writes use SVN; update commands keep their HTTP/HTTPS URLs.'}
            context={'work':str(area),'backend':backend,'database':database,'identity_domain':domain,
                     'fsfs_control_work':str(work/'fsfs'),
                     'manifest_sha256':value['inputs']['manifest_sha256'],'snapshot':snapshot['root'],
                     'snapshot_manifest_sha256':snapshot['manifest_sha256'],'suite':value['suite'],
                     'cases':value['cases'],'url':f'{protocol}://'+('localhost' if tls else '127.0.0.1')+f':{number}',
                     'httpd_version':value['inputs'].get('http',{}).get('apache_version'),
                     'local_fixture_url':local_inputs['url'] if local_inputs else None,
                     'tls_authority':tls['authority'] if tls else None}
            configuration=area/'entry.json';configuration.write_text(json.dumps(context,indent=2)+'\n',encoding='utf-8')
            child=OwnedProcess([sys.executable,'-I','-B',ROOT/'tests/pgvn/upstream_network_entry.py',configuration],
                               area,env,area/'upstream.log');processes.append(child)
            child.process.wait(timeout=1800);child_cleanup=child.stop();cleanup.append(child_cleanup)
            result_path=area/'entry-result.json'
            round_result={'backend':backend,'child_exit_code':child_cleanup['exit_code'],
                          'log':str(area/'upstream.log'),'log_sha256':sha(area/'upstream.log'),
                          'server_inputs':server_inputs,'tls_verification':tls_check,'local_fixture_server':local_inputs}
            if result_path.exists():round_result.update(read(result_path));round_result['result_sha256']=sha(result_path)
            else:round_result.update(status='FAIL',error='Entry terminated before producing an outcome')
            rounds.append(round_result)
            if local_server:cleanup.append(local_server.stop())
            cleanup.append(server.stop())
        assert identity(protocol)==value['inputs'],'Frozen network inputs changed during execution'
        assert all(r['status']=='PASS' for r in rounds),'At least one original upstream round failed'
        assert len(rounds)==2
        status='PASS'
    except BaseException:error=traceback.format_exc();print(error,flush=True)
    finally:
        for process in reversed(processes):
            if not process.closed:
                try:cleanup.append(process.stop())
                except BaseException:
                    status='FAIL';error=(error or '')+'\nCleanup: '+traceback.format_exc()
    report={'test_id':value['test_id'],'status':status,'component':'M6 original upstream network entry','protocol':protocol,
            'milestone_complete':False,'pgvn_601_complete':False,'database':database,'work':str(work),
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'inputs':value['inputs'],'plan_sha256':sha(path),
            'suite':value['suite'],'selected_cases':value['upstream_cases'],'snapshot':snapshot,'rounds':rounds,
            'counts_by_backend':{r['backend']:dict(Counter(c['result'] for c in r.get('cases',[]))) for r in rounds},
            'upstream_python_case_outcomes':sum(len(r.get('cases',[])) for r in rounds),
            'upstream_python_tests_executed':sum(c['result']!='SKIP' for r in rounds for c in r.get('cases',[])),
            'cleanup':cleanup,'error':error,'elapsed_seconds':time.monotonic()-start,
            'expected_job_count':len(processes),'all_owned_jobs_closed':all(p.closed for p in processes),
            'scope':'Explicit original assertions and decorators over '+protocol+' with a stock FSFS control; remaining protocol/suite matrix and full applicability ledger are pending.',
            'monitor_tokens_created':0,'temporary_windows_services_created':0,'databases_and_roles_retained':True}
    save(report,'m6-network');return 0 if status=='PASS' else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__);modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true');modes.add_argument('--execute-plan')
    parser.add_argument('--suite',default='basic_tests');parser.add_argument('--cases',default='1:12')
    parser.add_argument('--protocol',choices=('svn','http','https'),default='svn')
    args=parser.parse_args()
    return plan(args) if args.plan else execute(args.execute_plan)

if __name__=='__main__':raise SystemExit(main())
