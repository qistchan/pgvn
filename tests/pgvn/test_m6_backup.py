"""Logical database backup/restore with durable DAV state and protected keys.

Only creates two unique databases in the verified dedicated 16432 fixture.
Never restarts the cluster and never accesses the installed Polarion service.
"""
from pathlib import Path
import base64
import datetime as dt
import hashlib
from functools import partial
import json
import os
import shutil
import subprocess
import time
import traceback
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import test_m3_credentials as setup
import test_m3_protocol_smoke as protocol
import test_m5_http as http
from test_m4 import build_identity
from test_m4_smoke import request,until,ready_status
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace('m3-credentials','m6-backup')
SUFFIX=TEST_ID.rsplit('-',1)[1]
setup.WORK=RUNTIME/'tests'/TEST_ID;setup.EMPTY=setup.WORK/'pathless-working-directory'
setup.DATABASE='pgvn_m6_backup_'+SUFFIX
protocol.WORK=setup.WORK;protocol.EMPTY=setup.EMPTY;protocol.PASSWORD=http.PASSWORD.encode()
client=partial(protocol.client,password=protocol.PASSWORD)
SOURCE=setup.DATABASE;TARGET='pgvn_m6_restore_'+SUFFIX;RESTORE_OWNER='pgvn_restore_'+SUFFIX
DETAILS={}

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def dsn(database,role='pgvn_admin'):return f'host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable'
def run(executable,*args,database=None,role='pgvn_admin',input=None,timeout=120):
    result=subprocess.run([str(executable),*map(str,args)],cwd=setup.EMPTY,
        env=dict(os.environ,PGVN_DSN=dsn(database or setup.DATABASE,role)),input=input,
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=timeout)
    assert result.returncode==0,(Path(executable).name,result.returncode,result.stderr[:1600])
    return result.stdout
def admin(*args,database=None,role='pgvn_admin'):
    return json.loads(run(setup.ADMIN,*args,'--json',database=database,role=role))
def sql(query,database):return setup.fixture.sql(query,database=database)
def merge(server,route,alias):
    body=('<D:merge xmlns:D="DAV:"><D:source><D:href>'+escape(server.url(route+'/!svn/vtxn/'+alias))+
        '</D:href></D:source><D:no-auto-merge/><D:no-checkout/><D:prop><D:checked-in/>'+
        '<D:version-name/><D:creationdate/><D:creator-displayname/></D:prop></D:merge>').encode()
    status,_,payload=server.request('MERGE',route,body,{'Content-Type':'text/xml'})
    assert status==200,(status,payload[:500])
def transaction(server,route,alias):
    status,_,payload=server.request('POST',route+'/!svn/me',b'( create-txn-with-props (svn:log restore-check) )',
        {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':alias})
    assert status==201,(status,payload[:500])
def put(server,route,alias,path,data):
    status,_,payload=server.request('PUT',route+'/!svn/vtxr/'+alias+'/'+path,data,{'Content-Type':'application/octet-stream'})
    assert status in (201,204),(status,payload[:500])
def row_ledger(database):
    # Hash complete rows, including bytea and SQL NULL, without serializing secrets.
    tables=json.loads(sql("SELECT json_agg(json_build_array(table_schema,table_name) ORDER BY table_schema,table_name) "
        "FROM information_schema.tables WHERE table_schema IN ('pgfs','pgvn','pgvn_monitor') AND table_type='BASE TABLE'",database))
    result={}
    for schema,table in tables:
        assert schema.replace('_','').isalnum() and table.replace('_','').isalnum()
        text=sql(f'SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\'::jsonb)::text FROM "{schema}"."{table}" t',database)
        result[schema+'.'+table]=hashlib.sha256(text.encode()).hexdigest()
    sequences=json.loads(sql("SELECT coalesce(json_agg(json_build_array(sequence_schema,sequence_name) ORDER BY sequence_schema,sequence_name),'[]'::json) "
        "FROM information_schema.sequences WHERE sequence_schema IN ('pgfs','pgvn')",database))
    for schema,name in sequences:
        assert schema.replace('_','').isalnum() and name.replace('_','').isalnum()
        result['sequence:'+schema+'.'+name]=sql(f'SELECT last_value,is_called FROM "{schema}"."{name}"',database)
    return result
def protect(path,service=False):
    args=['icacls.exe',str(path),'/inheritance:r','/grant:r','*S-1-5-32-544:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'] if path.is_dir() else [
        'icacls.exe',str(path),'/inheritance:r','/grant:r','*S-1-5-32-544:F','*S-1-5-18:F']
    if service:args.append('*S-1-5-19:R')
    result=subprocess.run(args,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
    assert result.returncode==0,(result.stdout,result.stderr)

def checks():
    frozen=build_identity();source_hash=sha(Path(__file__))
    setup.setup_database();setup.fixture.assert_ours()
    key=setup.WORK/'credentials-v1.key';admin('key','create',key,'backup-test-v1')
    for user in ('writer','reader'):setup.admin('user','set','pg vn test',user,'--key-file',key,input=protocol.PASSWORD)
    repo=admin('repo','create','backup-history','--route','/svn/restore')
    route='/svn/restore';rules=setup.WORK/'authz.conf';rules.write_text('[/]\nwriter = rw\nreader = r\n',encoding='utf-8')
    hooks=setup.WORK/'hooks.json';hooks.write_text('{"pre-revprop-change":"allow"}',encoding='utf-8')
    admin('config','publish',repo['id'],'--expected-generation',1,'--authz',rules,'--hooks',hooks)
    issued=admin('monitor-token','create','--label','backup-restore-check','--expires-in',1800)
    a=protocol.Node('source-svn',key);dav=http.Apache('source-dav');target_node=None;target_dav=None
    payload=bytes(range(256))*4097+b'backup\x00\xff\r\n';data=setup.WORK/'content.bin';data.write_bytes(payload)
    binary_prop=setup.WORK/'revprop.bin';binary_prop.write_bytes(b'revision property\x00\xff')
    try:
        a.start();dav.start();until(lambda:ready_status(a)==200)
        client('svnmucc','-m','backup r1','put',data,a.url(route+'/payload.bin'))
        client('svn','propset','--revprop','-r','1','pgvn:binary','--file',binary_prop,a.url(route))
        client('svn','lock',a.url(route+'/payload.bin'),'-m','preserved lock')
        old_lock=ET.fromstring(client('svn','info','--xml',a.url(route+'/payload.bin'))).find('.//lock/token').text
        transaction(dav,route,'restore-committed');put(dav,route,'restore-committed','receipt.bin',b'receipt\x00\xff')
        merge(dav,route,'restore-committed');merge(dav,route,'restore-committed')
        assert admin('look','youngest',repo['id'])['youngest']==2
        committed_id=sql("SELECT txn_id FROM pgvn.dav_bindings WHERE binding_name='restore-committed'",SOURCE)
        transaction(dav,route,'restore-pending');put(dav,route,'restore-pending','before-backup.bin',b'before backup\x00')
        pending_id=sql("SELECT txn_id FROM pgvn.dav_bindings WHERE binding_name='restore-pending'",SOURCE)
        assert sql("SELECT creation_phase FROM pgvn.dav_transactions WHERE txn_id='"+pending_id+"'",SOURCE)=='active'
        snapshot=run(setup.PROBE,'snapshot',repo['id'],2)
        assert request(a,token=issued['token'])[0]['ready']
        dav.close();a.stop()
        original_ledger=row_ledger(SOURCE)
        backup=setup.WORK/'backup';backup.mkdir();protect(backup)
        archive=backup/'database.dump';pg=setup.fixture.pg
        print('Dumping the isolated source database with the non-superuser admin role',flush=True)
        run(pg/'bin/pg_dump.exe','--format=custom','--file',archive,'--dbname',dsn(SOURCE),timeout=180)
        secrets_dir=backup/'protected-config';secrets_dir.mkdir()
        shutil.copyfile(key,secrets_dir/key.name)
        shutil.copyfile(dav.work/'users.htpasswd',secrets_dir/'users.htpasswd')
        shutil.copyfile(rules,secrets_dir/'authz.conf');shutil.copyfile(hooks,secrets_dir/'hooks.json')
        files={p.name:sha(p) for p in secrets_dir.iterdir() if p.is_file()}
        setup.fixture.sql(f'CREATE ROLE {RESTORE_OWNER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',database='postgres')
        setup.fixture.sql(f'CREATE DATABASE {TARGET} OWNER {RESTORE_OWNER} ENCODING \'UTF8\' TEMPLATE template0',database='postgres')
        print('Restoring into a different database owned by a different non-superuser',flush=True)
        # Keep the archive's ACLs. --no-acl would expose default PUBLIC function
        # grants unless all of those revocations were deliberately reconstructed.
        run(pg/'bin/pg_restore.exe','--exit-on-error','--no-owner','--dbname',dsn(TARGET,RESTORE_OWNER),archive,timeout=180)
        assert row_ledger(TARGET)==original_ledger,'Complete row/sequence ledger changed in restore'
        admin('db','check',database=TARGET)
        admin('db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor',database=TARGET,role=RESTORE_OWNER)
        assert row_ledger(TARGET)==original_ledger
        assert sql("SELECT rolsuper FROM pg_roles WHERE rolname='"+RESTORE_OWNER+"'",TARGET)=='f'
        assert sql("SELECT NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
            "WHERE n.nspname='pgvn_monitor' AND p.proname='publish' AND a.grantee=0 AND a.privilege_type='EXECUTE')",TARGET)=='t'
        assert sql("SELECT has_table_privilege('pgvn_monitor','pgvn.credentials','SELECT')",TARGET)=='f'
        assert sql("SELECT has_function_privilege('pgvn_monitor','pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb)','EXECUTE')",TARGET)=='f'
        restored=setup.WORK/'restored-config';restored.mkdir();protect(restored)
        restored_key=restored/key.name;shutil.copyfile(secrets_dir/key.name,restored_key);protect(restored_key,service=True)
        assert sha(restored_key)==files[key.name]
        setup.DATABASE=TARGET
        digest=json.loads(run(setup.PROBE,'credential-digest',restored_key,'pg vn test','writer',role='pgvn_service'))
        assert digest['sha1']==hashlib.sha1(protocol.PASSWORD).hexdigest()
        assert run(setup.PROBE,'snapshot',repo['id'],2)==snapshot
        target_node=protocol.Node('restored-svn',restored_key);target_dav=http.Apache('restored-dav')
        shutil.copyfile(secrets_dir/'users.htpasswd',target_dav.work/'users.htpasswd')
        assert sha(target_dav.work/'users.htpasswd')==files['users.htpasswd']
        target_node.start();target_dav.start();until(lambda:ready_status(target_node)==200)
        assert client('svn','cat',target_node.url(route+'/payload.bin'))==payload
        new_lock=ET.fromstring(client('svn','info','--xml',target_node.url(route+'/payload.bin'))).find('.//lock/token').text
        assert new_lock==old_lock
        assert http.svn('cat',target_dav.url(route+'/payload.bin'))==payload
        assert client('svn','propget','--strict','--revprop','-r','1','pgvn:binary',target_node.url(route))==binary_prop.read_bytes()
        overview,_=request(target_node,token=issued['token']);assert overview['ready']
        assert run(setup.PROBE,'receipt',repo['id'],'writer',committed_id,role='pgvn_service').strip()==b'revision=2'
        # Continue an in-flight DAV transaction after changing both DB and node.
        put(target_dav,route,'restore-pending','after-restore.bin',b'after restore\x00\xff')
        merge(target_dav,route,'restore-pending');merge(target_dav,route,'restore-pending')
        assert admin('look','youngest',repo['id'])['youngest']==3
        assert client('svn','cat',target_node.url(route+'/before-backup.bin'))==b'before backup\x00'
        assert client('svn','cat',target_node.url(route+'/after-restore.bin'))==b'after restore\x00\xff'
        merge(target_dav,route,'restore-committed');assert admin('look','youngest',repo['id'])['youngest']==3
        client('svnmucc','-m','reader denied','mkdir',target_node.url(route+'/forbidden'),user='reader',expected=1)
        assert admin('verify',repo['id'])['verified_revisions']==4
        assert row_ledger(SOURCE)==original_ledger,'Restored writes modified the source database'
        assert not any(setup.EMPTY.iterdir())
        DETAILS.update(source_database=SOURCE,target_database=TARGET,source_head=2,restored_head_after_resume=3,
            repository_identity_preserved=True,public_uuid=repo['uuid'],logical_tables_and_sequences=len(original_ledger),
            logical_row_ledger_sha256=hashlib.sha256(json.dumps(original_ledger,sort_keys=True).encode()).hexdigest(),
            full_row_and_sequence_equality=True,archive_sha256=sha(archive),archive_bytes=archive.stat().st_size,
            protected_config_file_hashes=files,encrypted_credential_authentication=True,monitor_token_survives=True,
            svn_lock_token_preserved=True,binary_revision_property_preserved=True,published_receipt_survives=True,
            pending_dav_transaction_resumes=True,repeated_merge_no_new_revision=True,reader_write_denied=True,
            public_telemetry_execute_denied=True,monitor_credentials_read_denied=True,source_unchanged=True,
            no_per_repository_directory=True,restore_owner_superuser=False,archive_acl_preserved=True,
            test_source_sha256=source_hash,build_manifest_sha256=sha(ROOT/'artifacts/pgvn/build/manifest.json'))
        assert build_identity()==frozen and sha(Path(__file__))==source_hash
        return DETAILS
    finally:
        if target_dav:target_dav.close()
        if target_node:target_node.stop()
        dav.close();a.stop()

def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=DETAILS;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M6 logical backup, protected keys and stateful restore','status':status,
        'milestone_complete':False,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'elapsed_seconds':time.monotonic()-start,'details':details,'error':error,
        'scope':'Two unique databases and independent service processes on the dedicated local cluster; not cross-host failover or a clean-machine test.'}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m6-backup-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({'test_id':TEST_ID,'status':status,'elapsed_seconds':report['elapsed_seconds']}),flush=True)
    return 0 if status=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
