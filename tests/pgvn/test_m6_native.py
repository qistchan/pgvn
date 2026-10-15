"""Unmodified upstream C cases against directory-free PgVN and stock FSFS.

Constructor/reopen fixture replacement is explicit. No assertions, verdicts,
error output or upstream case modes are rewritten. This is an M6 component.
"""
from pathlib import Path
import argparse
from collections import Counter
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
from test_m4 import build_identity
from native_applicability import CASE_COUNTS,CASE_NAMES,EXCLUSIONS

TEST_ID='m6-native-'+uuid.uuid4().hex[:12]
WORK=RUNTIME/'tests'/TEST_ID
RESULTS=ROOT/'artifacts/pgvn/tests'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=('fs','repos','all'),default='all')
    parser.add_argument('--backend',choices=('pg','fsfs','directory-fsfs','both'),default='both')
    parser.add_argument('--case',type=int,action='append')
    parser.add_argument('--applicable',action='store_true',help='Explicitly classify directory-format/file-RA/global-loader cases; stock FSFS still runs every case')
    args=parser.parse_args()
    if args.case and (args.suite=='all' or any(n<=0 for n in args.case)):parser.error('Positive case selections require a single suite')
    if args.case and args.applicable:parser.error('Custom case selection and complete applicable matrix are distinct profiles')
    if args.backend=='directory-fsfs' and args.suite=='fs':parser.error('The directory-mode control covers the repos suite')
    if args.case and any(n>CASE_COUNTS[args.suite] for n in args.case):parser.error('Case number exceeds the pinned upstream inventory')
    start=time.monotonic();records=[];details={};WORK.mkdir(parents=True)
    try:
        manifest=build_identity();frozen=sha(ROOT/'artifacts/pgvn/build/manifest.json')
        native_path=ROOT/'artifacts/pgvn/build/native-tests.json'
        native_hash=sha(native_path)
        native=json.loads(native_path.read_text(encoding='utf-8'))
        assert native['build_manifest_sha256']==frozen
        assert native['core_archive_sha256']==sha(ROOT/'build/pgvn/libpgvn_core.a')==manifest['core_archive_sha256']
        for name,expected in native['runtime_dependencies'].items():assert sha(ROOT/name)==expected,'Native test DLL changed: '+name
        for metadata in native['products'].values():assert sha(ROOT/metadata['path'])==metadata['sha256']
        for suite,count in CASE_COUNTS.items():
            assert len(native['case_names'][suite])==count
            for number,name in CASE_NAMES[suite].items():assert native['case_names'][suite][number-1]==name
        fixture=Fixture();fixture.initialize();fixture.assert_ours()
        suffix=TEST_ID.rsplit('-',1)[1];database='pgvn_m6_native_'+suffix;owner='pgvn_native_owner_'+suffix
        fixture.sql(f'CREATE ROLE {owner} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',database='postgres')
        fixture.sql(f"CREATE DATABASE {database} OWNER {owner} ENCODING 'UTF8' TEMPLATE template0",database='postgres')
        def dsn(role):return f'host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable'
        empty=WORK/'admin-working-directory';empty.mkdir()
        for command in (['db','init'],['db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor']):
            completed=subprocess.run([str(RUNTIME/'bin/pgvnadmin.exe'),*command,'--json'],cwd=empty,
                env=dict(os.environ,PGVN_DSN=dsn(owner)),capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=45)
            assert completed.returncode==0,completed.stderr.decode('utf-8','replace')
        assert fixture.sql("SELECT count(*) FROM pgvn.repositories",database=database)=='0'
        if args.backend in ('pg','both'):
            probe_work=WORK/'pg-path-probe';probe_work.mkdir();probe_ledger=probe_work/'fixture-ledger.jsonl'
            probe_metadata=native['products']['path-probe'];probe_binary=ROOT/probe_metadata['path'];assert sha(probe_binary)==probe_metadata['sha256']
            probe=subprocess.run([str(probe_binary)],cwd=probe_work,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=45,
                env=dict(os.environ,PGVN_DSN=dsn('pgvn_admin'),PGVN_NATIVE_TEST_DATABASE=database,
                    PGVN_NATIVE_TEST_ROOT=probe_work.as_posix(),PGVN_NATIVE_TEST_LEDGER=str(probe_ledger)))
            assert probe.returncode==0,probe.stderr.decode('utf-8','replace')
            path_probe={'result':json.loads(probe.stdout),'test_binary_sha256':sha(probe_binary),'source_sha256':probe_metadata['source_sha256'],
                'fixture_ledger_sha256':sha(probe_ledger),'no_local_repository_alias':not (probe_work/'native-path-apis').exists()}
        else:path_probe=None
        selected=('fs','repos') if args.suite=='all' else (args.suite,)
        backends=('pg','fsfs','directory-fsfs') if args.backend=='both' else (args.backend,)
        for backend in backends:
            for suite in selected:
                if backend=='directory-fsfs' and suite!='repos':continue
                key=suite if backend=='pg' else ('directory-fsfs-repos' if backend=='directory-fsfs' else 'fsfs-'+suite);metadata=native['products'][key]
                binary=ROOT/metadata['path'];assert sha(binary)==metadata['sha256']
                work=WORK/(backend+'-'+suite);work.mkdir();ledger=work/'fixture-ledger.jsonl'
                env=dict(os.environ,PGVN_DSN=dsn('pgvn_admin'),PGVN_NATIVE_TEST_DATABASE=database,
                    PGVN_NATIVE_TEST_ROOT=work.as_posix(),PGVN_NATIVE_TEST_LEDGER=str(ledger))
                source=Path(manifest['inputs']['subversion'])/'subversion/tests'/('libsvn_'+suite)
                excluded=EXCLUSIONS[suite] if args.applicable and backend=='pg' else {}
                selection=args.case or ([n for n in range(1,CASE_COUNTS[suite]+1) if n not in excluded] if excluded else [])
                command=[str(binary),'--fs-type='+('pg' if backend=='pg' else 'fsfs'),'--srcdir='+str(source),*map(str,selection)]
                before=time.monotonic();log=work/'upstream.log'
                with log.open('wb') as stream:
                    process=subprocess.run(command,cwd=work,env=env,stdout=stream,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,timeout=1200)
                text=log.read_text(encoding='utf-8',errors='replace')
                counts=dict(Counter(re.findall(r'^(PASS|FAIL|SKIP|XFAIL|XPASS):',text,re.M)))
                assert counts,'Native program returned no test verdicts: '+text[-2000:]
                entries=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
                for entry in entries:
                    assert entry['process_code_page']==65001
                    alias=bytes.fromhex(entry['alias_hex']).decode('utf-8')
                    if not alias.startswith('pgvn:'):
                        path=Path(alias);path=path if path.is_absolute() else work/path
                        assert path.resolve().is_relative_to(work.resolve())
                        assert not path.exists(),'Native repository alias became a local file/directory: '+str(path)
                if backend=='pg' and not args.case:assert any(e['operation']=='create' for e in entries)
                shells=[p for p in work.rglob('*') if p.is_dir() and
                    ((p/'db/fs-type').exists() or all((p/n).is_dir() for n in ('db','conf','hooks','locks')))] if backend=='pg' else []
                assert not shells,'Untraced local repository shells: '+str(shells)
                record={'suite':suite,'backend':backend,'status':'PASS' if process.returncode==0 and not counts.get('FAIL') and not counts.get('XPASS') else 'FAIL',
                    'exit_code':process.returncode,'selection':selection or 'all upstream cases','counts':counts,'elapsed_seconds':round(time.monotonic()-before,3),
                    'upstream_case_count':CASE_COUNTS[suite],'exclusions':{n:{'name':CASE_NAMES[suite][n],'reason':reason} for n,reason in excluded.items()},
                    'test_binary_sha256':sha(binary),'test_source_sha256':metadata['source_sha256'],'log':log.relative_to(ROOT).as_posix(),'log_sha256':sha(log),
                    'fixture_ledger_sha256':sha(ledger) if ledger.exists() else None,'fixture_operations':dict(Counter(e['operation'] for e in entries)),
                    'no_local_repository_aliases':backend=='pg','unexpected_verdicts':[line for line in text.splitlines() if line.startswith(('FAIL:','XPASS:'))]}
                record['untraced_repository_shells']=[str(p) for p in shells]
                records.append(record);print(json.dumps(record),flush=True)
        details={'database':database,'owner':owner,'native_manifest_sha256':sha(native_path),'build_manifest_sha256':frozen,
            'core_archive_sha256':native['core_archive_sha256'],'source_scope':native['scope'],'native_test_selection_complete':not args.case and args.suite=='all' and args.backend=='both',
            'applicable_profile':args.applicable,'path_probe':path_probe,'full_native_upstream_compatibility':False}
        assert build_identity()==manifest and sha(native_path)==native_hash
        for name,expected in native['runtime_dependencies'].items():assert sha(ROOT/name)==expected,'Native test DLL changed: '+name
        assert all(record['status']=='PASS' for record in records),'Unmodified upstream assertions failed; see retained logs'
        status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M6 native upstream FS/repos','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),
        'records':records,'details':details,'error':error,'test_source_sha256':sha(Path(__file__)),
        'scope':'Native C API component only. PgVN construction uses the production static core and administrative ID bindings. Stock FSFS is an independent control. No directory-format/protocol/performance/HA/Polarion completion is inferred.'}
    RESULTS.mkdir(parents=True,exist_ok=True);body=json.dumps(report,indent=2)+'\n'
    (RESULTS/(TEST_ID+'.json')).write_text(body,encoding='utf-8');(RESULTS/'m6-native-latest.json').write_text(body,encoding='utf-8')
    print(json.dumps({'test_id':TEST_ID,'status':status,'groups':len(records)}),flush=True)
    return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
