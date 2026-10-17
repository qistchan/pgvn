"""Upgrade a real M4 schema-5 repository using the saved tested M4 admin."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import traceback
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-upgrade')
DATABASE=setup.DATABASE+'_upgrade'
BASELINE=RUNTIME/'baselines/m4-b67d041'


def admin(executable,*args,role='pgvn_admin',expected=0,input=None):
    dsn=setup.dsn(role).replace('dbname='+setup.DATABASE+' ','dbname='+DATABASE+' ')
    result=subprocess.run([str(executable),*map(str,args),'--json'],input=input,cwd=setup.EMPTY,
        env=dict(os.environ,PGVN_DSN=dsn),capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=40)
    assert result.returncode==expected,(args,result.returncode,result.stdout[:1000],result.stderr[:1500])
    return json.loads(result.stdout if expected==0 else result.stderr)


def checks():
    setup.setup_database();setup.fixture.assert_ours()
    old=BASELINE/'bin/pgvnadmin.exe';manifest=json.loads((BASELINE/'manifest.json').read_text())
    assert hashlib.sha256(old.read_bytes()).hexdigest()==manifest['products']['runtime/pgvn/bin/pgvnadmin.exe']['sha256']
    # Produce an ordinary SVN history with the new binary; its dump stream is
    # a protocol-independent way to place binary/author/history in the M4 DB.
    source=setup.admin('repo','create','upgrade-source','--route','/upgrade-source')
    rules=setup.WORK/'rules.conf';rules.write_text('[/]\nwriter = rw\n',encoding='utf-8')
    setup.admin('config','publish',source['id'],'--expected-generation',1,'--authz',rules)
    data=setup.WORK/'payload.bin';data.write_bytes(b'M4 to M5\0\xff\r\n')
    txn=setup.run(setup.PROBE,['begin',source['id'],'writer'],role='pgvn_service').stdout.strip().decode()
    setup.run(setup.PROBE,['put',source['id'],'writer',txn,'/payload.bin',data],role='pgvn_service')
    setup.run(setup.PROBE,['commit',source['id'],'writer',txn],role='pgvn_service')
    dump=setup.run(setup.ADMIN,['dump',source['id']],role='pgvn_admin').stdout
    setup.fixture.sql(f"CREATE DATABASE {DATABASE} OWNER {setup.OWNER} ENCODING 'UTF8' TEMPLATE template0",database='postgres')
    admin(old,'db','init',role=setup.OWNER)
    assert admin(old,'db','check',role=setup.OWNER)['schema']['pgvn']==5
    admin(old,'db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor',role=setup.OWNER)
    repo=admin(old,'repo','create','upgrade-target','--route','/upgrade-target')
    admin(old,'repo','disable',repo['id'])
    admin(old,'load',repo['id'],input=dump)
    admin(old,'repo','enable',repo['id'])
    admin(old,'verify',repo['id'])
    original=admin(old,'repo','show',repo['id'])
    assert admin(old,'look','youngest',repo['id'])['youngest']==1
    before=setup.fixture.sql("SELECT string_agg(version||':'||migration_sha256,',' ORDER BY version) FROM pgvn.schema_versions",database=DATABASE)
    migrated=admin(setup.ADMIN,'db','migrate',role=setup.OWNER)
    assert migrated['ok']
    assert admin(setup.ADMIN,'db','check',role=setup.OWNER)['schema']['pgvn']==setup.CATALOG_VERSION
    admin(setup.ADMIN,'db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor',role=setup.OWNER)
    after=admin(setup.ADMIN,'repo','show',repo['id'])
    assert after==original,(after,original)
    assert setup.fixture.sql("SELECT string_agg(version||':'||migration_sha256,',' ORDER BY version) FROM pgvn.schema_versions WHERE version<=5",database=DATABASE)==before
    assert setup.fixture.sql("SELECT count(*) FROM pgvn.dav_transactions",database=DATABASE)=='0'
    assert setup.fixture.sql("SELECT has_table_privilege('pgvn_service','pgvn.dav_bindings','INSERT')",database=DATABASE)=='t'
    admin(setup.ADMIN,'verify',repo['id'])
    # Binary look output does not accept --json; keep its data separate from
    # metadata commands and compare exact bytes after migration.
    dsn=setup.dsn('pgvn_admin').replace('dbname='+setup.DATABASE+' ','dbname='+DATABASE+' ')
    cat=subprocess.run([str(setup.ADMIN),'look','cat',repo['id'],'payload.bin'],cwd=setup.EMPTY,
        env=dict(os.environ,PGVN_DSN=dsn),capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    assert cat.returncode==0 and cat.stdout==data.read_bytes(),cat.stderr
    admin(old,'db','check',expected=4)
    checksum=setup.fixture.sql('SELECT migration_sha256 FROM pgvn.schema_versions WHERE version=6',database=DATABASE)
    try:
        setup.fixture.sql("UPDATE pgvn.schema_versions SET migration_sha256=repeat('0',64) WHERE version=6",database=DATABASE)
        admin(setup.ADMIN,'db','check',expected=4)
    finally:setup.fixture.sql("UPDATE pgvn.schema_versions SET migration_sha256='"+checksum+"' WHERE version=6",database=DATABASE)
    assert admin(setup.ADMIN,'db','check')['schema']['pgvn']==setup.CATALOG_VERSION
    return {'saved_m4_binary_verified':True,'actual_schema5_created_by_m4':True,'schema5_to_current':True,
        'old_migration_checksums_unchanged':True,'repository_identity_and_history_preserved':True,
        'binary_content_preserved':True,'service_privileges_updated':True,'m4_rejects_current_schema':True,
        'tampered_migration6_rejected':True,'full_verify':True}


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details={};status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 schema upgrade','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,'elapsed_seconds':time.monotonic()-start,
        'pgvnadmin_sha256':hashlib.sha256(setup.ADMIN.read_bytes()).hexdigest()}
    output=ROOT/'artifacts/pgvn/tests';output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (output/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(output/'m5-upgrade-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
