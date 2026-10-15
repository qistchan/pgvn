"""Real APR/libpq environment and Unicode-path integration on Windows."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from fixture import Fixture,ROOT,RUNTIME
from test_m4 import build_identity

sys.path.insert(0,str(ROOT/'scripts/pgvn'))
from httpd_host import compile_resource
from windows_manifest import embed_utf8
from pe_dependencies import closure,imports

TEST_ID='m6-windows-utf8-'+uuid.uuid4().hex[:12]
WORK=RUNTIME/'tests'/TEST_ID
RESULTS=ROOT/'artifacts/pgvn/tests'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(arguments,env=None,timeout=30,expected=0):
    value=subprocess.run(list(map(str,arguments)),cwd=WORK,env=env,capture_output=True,
        timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW)
    if value.returncode!=expected:
        raise AssertionError((Path(arguments[0]).name,value.returncode,value.stderr[-2500:].decode('utf-8','replace')))
    return value.stdout


def profile(directory,role):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'pg_service.conf'
    path.write_text('[pgvn_service]\nhost=127.0.0.1\nport=16432\ndbname=pgvn_poc\nuser='+role+'\nsslmode=disable\n',encoding='utf-8')
    environment=dict(os.environ,PATH=str(Path(os.environ['SystemRoot'])/'System32'),PGSERVICEFILE=str(path))
    for key in ('PGPASSWORD','PGPASSFILE','PGSERVICE','PGVN_DSN'):environment.pop(key,None)
    return environment


def checks():
    WORK.mkdir(parents=True);(WORK/'generated').mkdir();(WORK/'objects').mkdir()
    identity=build_identity()
    fixture=Fixture();fixture.initialize();fixture.assert_ours()
    assert fixture.sql("SELECT rolsuper FROM pg_roles WHERE rolname='pgvn_admin'")=='f'
    tools=Path(identity['inputs']['toolchain']);pg=Path(identity['inputs']['postgres'])
    assert sha(tools/'clang.exe')==identity['compiler_sha256']
    options=['-std=c17','-O2','-DWIN32','-D_M_X64','-I',ROOT/'build/pgvn/generated',
        '-I',Path(identity['inputs']['apr_include']),'-I',pg/'include']
    source=ROOT/'tests/pgvn/utf8_env_probe.c'
    baseline=WORK/'plain.exe';enabled=WORK/'utf8.exe'
    libraries=[ROOT/'build/pgvn/imports/libapr-1.a',pg/'lib/libpq.lib']
    run([tools/'clang.exe',*options,source,*libraries,'-Wl,--no-insert-timestamp','-o',baseline])
    resource=compile_resource(ROOT,WORK,tools,run)
    run([tools/'clang.exe',*options,source,*libraries,resource,'-Wl,--no-insert-timestamp','-o',enabled])
    dependencies,_=closure([baseline,enabled],[RUNTIME/'apache/bin'])
    for path in dependencies.values():
        if path.suffix.lower()=='.dll':shutil.copyfile(path,WORK/path.name)
    ascii_env=profile(WORK/'ascii','pgvn_service')
    plain=[json.loads(line) for line in run([baseline,'app'],ascii_env).splitlines()]
    acp=plain[0]['acp'];negative={'system_process_code_page':acp,'observed':False}
    if acp!=65001:
        selected=None
        for character in ('é','日','가','Ж','א','ا','ก'):
            try:character.encode('cp'+str(acp));selected=character;break
            except (LookupError,UnicodeEncodeError):pass
        if selected:
            negative_env=profile(WORK/('legacy-'+selected),'pgvn_service')
            observed=[json.loads(line) for line in run([baseline,'app'],negative_env).splitlines()]
            assert observed[0]['pq_ok']==1 and observed[0]['wide_read']==1
            assert observed[1]['getenv_utf8']==1 and observed[1]['pq_ok']==0 and observed[1]['pq_service_file_error']==1
            negative.update(observed=True,observations=observed)
        else:negative['reason']='No candidate representable in this process legacy code page'
    else:negative['reason']='The operating system process already uses UTF-8; the legacy mismatch is absent'
    positive_env=profile(WORK/'完整 中文 🐘 é e\u0301','pgvn_service')
    positive=[json.loads(line) for line in run([enabled,'app'],positive_env).splitlines()]
    assert all(item['acp']==65001 and item['pq_ok']==1 and item['wide_read']==1 and item['fopen_utf8']==1 for item in positive)
    print('APR/libpq legacy mismatch and per-process UTF-8 behavior verified',flush=True)
    # Preserve signed SDK images even when the caller asks for a new manifest.
    signed=WORK/'signed-sdk-httpd.exe'
    shutil.copyfile(RUNTIME/'apache/bin/httpd.exe',signed);original=sha(signed)
    try:embed_utf8(signed,ROOT/'src/pgvn/windows/utf8.manifest')
    except ValueError as error:assert 'signed SDK executable' in str(error)
    else:raise AssertionError('Signed SDK executable was rewritten')
    assert sha(signed)==original==identity['apache_host']['sdk_host_sha256']
    original_pg=pg/'bin/pg_dump.exe';source_hash=sha(original_pg)
    derived=[];before_imports=imports(original_pg)
    for label in ('a','b'):
        target=WORK/('pg_dump_utf8_'+label+'.exe');shutil.copyfile(original_pg,target)
        transform=embed_utf8(target,ROOT/'src/pgvn/windows/utf8.manifest')
        assert imports(target)==before_imports and transform['non_resource_bytes_unchanged']
        assert transform['program_code_data_rvas_unchanged']
        stable=embed_utf8(target,ROOT/'src/pgvn/windows/utf8.manifest')
        assert not stable['changed']
        derived.append((target,transform))
    assert sha(derived[0][0])==sha(derived[1][0]) and sha(original_pg)==source_hash
    dependencies,_=closure([derived[0][0]],[pg/'bin',RUNTIME/'bin'])
    for path in dependencies.values():
        if path.suffix.lower()=='.dll':
            target=WORK/path.name
            if target.exists():assert sha(target)==sha(path)
            else:shutil.copyfile(path,target)
    admin_env=profile(WORK/'管理 🐘','pgvn_admin')
    output=WORK/'备份 🐘.sql'
    run([derived[0][0],'--schema-only','--no-owner','--no-privileges','--dbname','service=pgvn_service','--file',output],admin_env,timeout=60)
    assert output.stat().st_size>0
    assert build_identity()==identity
    return {'build_manifest_sha256':sha(ROOT/'artifacts/pgvn/build/manifest.json'),
        'legacy_control':negative,'utf8_observations':positive,'signed_sdk_refused_unchanged':True,
        'unsigned_pg_source_sha256':source_hash,'unsigned_pg_source_unchanged':True,'transform':derived[0][1],
        'unsigned_transform_reproducible_and_idempotent':True,'imports_unchanged':True,
        'unicode_supplementary_service_and_output_paths':True,'schema_dump_sha256':sha(output),
        'schema_dump_bytes':output.stat().st_size,'backup_role_superuser':False,
        'probe_source_sha256':sha(source),'test_source_sha256':sha(Path(__file__))}


def main():
    start=time.monotonic();details={}
    try:details=checks();status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M6 Windows process encoding integration','status':status,
        'milestone_complete':False,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'elapsed_seconds':time.monotonic()-start,'details':details,'error':error,
        'scope':'Dedicated local PostgreSQL/APR and native Unicode-file integration. Full services and package restore are separate M7 checks.'}
    RESULTS.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (RESULTS/(TEST_ID+'.json')).write_text(data,encoding='utf-8')
    (RESULTS/'m6-windows-utf8-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({'test_id':TEST_ID,'status':status,'elapsed_seconds':report['elapsed_seconds']}),flush=True)
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
