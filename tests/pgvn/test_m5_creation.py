"""M5 foundation: real atomic FS creation + binding, not a DAV milestone gate."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import traceback
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-creation')
PROBE=RUNTIME/'bin/dav_state_probe.exe'


def literal(text): return "'"+text.replace("'","''")+"'"


def until(predicate,seconds=15):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.1)
    raise AssertionError('Expected native creation state did not arrive')


def command(repo,binding,mode,expected=0):
    result=setup.run(PROBE,[repo['id'],binding,mode],role='pgvn_service',expected=expected)
    data=json.loads(result.stdout)
    assert data['ok']==(expected==0),data
    if expected: assert data['unchanged_on_error'] and b'PgVN probe:' in result.stderr
    return data


def launch(repo,binding,mode):
    return subprocess.Popen([str(PROBE),repo['id'],binding,mode],cwd=setup.EMPTY,
        env=dict(os.environ,PGVN_DSN=setup.dsn('pgvn_service')),stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)


def ready(process):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(process.stdout.readline)
        try:line=future.result(timeout=10)
        except Exception:process.kill();process.wait(timeout=5);raise
    assert line.strip()==b'READY',(line,process.poll())


def checks():
    setup.setup_database();setup.fixture.assert_ours()
    repo=setup.admin('repo','create','M5 atomic creation','--route','/m5-creation')
    rules=setup.WORK/'rules.conf';rules.write_text('[/]\nwriter = rw\n',encoding='utf-8')
    setup.admin('config','publish',repo['id'],'--expected-generation',1,'--authz',rules)
    # Test-only binding table exercises the same connection/FK/unique-key
    # semantics that production DAV bindings will need in migration 006.
    setup.sql("CREATE TABLE pgvn.m5_binding_probe(repository_id uuid NOT NULL,binding text COLLATE \"C\" NOT NULL,"
        "txn_id text NOT NULL,PRIMARY KEY(repository_id,binding),FOREIGN KEY(repository_id,txn_id) REFERENCES pgfs.transactions,"
        "CHECK(octet_length(binding)<=256)); GRANT SELECT,INSERT ON pgvn.m5_binding_probe TO pgvn_service")
    command(repo,'rolled-back','rollback',expected=1)
    assert setup.sql('SELECT count(*) FROM pgfs.transactions')=='0'
    assert setup.sql('SELECT count(*) FROM pgfs.transaction_properties')=='0'
    assert setup.sql('SELECT count(*) FROM pgvn.m5_binding_probe')=='0'
    command(repo,'nested','nested',expected=1)
    assert setup.sql('SELECT count(*) FROM pgfs.transactions')=='0'
    name="创建 ' / 名称"
    created=command(repo,name,'success');txn=created['transaction_id']
    assert setup.sql('SELECT txn_id FROM pgvn.m5_binding_probe WHERE binding='+literal(name))==txn
    assert setup.sql("SELECT encode(value,'hex') FROM pgfs.transaction_properties WHERE txn_id='"+txn+"' AND name='pgvn:binary'")=='6200ff79'
    assert setup.sql("SELECT convert_from(value,'UTF8') FROM pgfs.transaction_properties WHERE txn_id='"+txn+"' AND name='svn:author'")=='writer'
    assert setup.sql("SELECT flags FROM pgfs.transactions WHERE txn_id='"+txn+"'")=='2'
    # A fresh process can open the committed staging transaction, use its
    # initial properties and publish through the existing repository layer.
    payload=setup.WORK/'payload.bin';payload.write_bytes(b'bound\0\xffpayload')
    setup.run(setup.PROBE,['put',repo['id'],'writer',txn,'/bound-file',payload],role='pgvn_service')
    published=setup.run(setup.PROBE,['commit',repo['id'],'writer',txn],role='pgvn_service')
    assert published.stdout.strip()==b'revision=1'
    assert setup.admin('look','youngest',repo['id'])['youngest']==1
    children=[]
    try:
        first=launch(repo,'collision','hold');children.append(first);ready(first)
        assert setup.sql("SELECT count(*) FROM pgvn.m5_binding_probe WHERE binding='collision'")=='0'
        assert setup.sql('SELECT count(*) FROM pgfs.transactions')=='1'
        second=launch(repo,'collision','success');children.append(second)
        until(lambda:int(setup.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn' AND wait_event_type='Lock'"))>=1)
        assert second.poll() is None
        output,error=first.communicate(b'\n',timeout=15)
        assert first.returncode==0,error
        winner=json.loads(output)
        output,error=second.communicate(timeout=15)
        assert second.returncode==1 and b'23505' in error,(second.returncode,output,error)
        assert json.loads(output)['unchanged_on_error']
        assert setup.sql("SELECT txn_id FROM pgvn.m5_binding_probe WHERE binding='collision'")==winner['transaction_id']
        assert setup.sql('SELECT count(*) FROM pgfs.transactions')=='2'
        assert setup.sql('SELECT count(*) FROM pgvn.m5_binding_probe')=='2'
        killed=launch(repo,'killed-before-commit','hold');children.append(killed);ready(killed)
        assert setup.sql("SELECT count(*) FROM pgvn.m5_binding_probe WHERE binding='killed-before-commit'")=='0'
        killed.kill();killed.communicate(timeout=10)
        until(lambda:setup.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn'")=='0')
        assert setup.sql('SELECT count(*) FROM pgfs.transactions')=='2'
        assert setup.sql('SELECT count(*) FROM pgvn.m5_binding_probe')=='2'
        assert setup.sql("SELECT count(*) FROM pgfs.transaction_properties p LEFT JOIN pgfs.transactions t USING(repository_id,txn_id) WHERE t.txn_id IS NULL")=='0'
    finally:
        for process in children:
            if process.poll() is None:process.kill();process.communicate(timeout=10)
    # UUID input casing cannot split the logical repository's session locks.
    txn=setup.run(setup.PROBE,['begin',repo['id'],'writer'],role='pgvn_service').stdout.strip().decode('ascii')
    writer=subprocess.Popen([str(setup.PROBE),'hold-writer',repo['id'],'writer',txn,'/case-file',str(payload)],
        cwd=setup.EMPTY,env=dict(os.environ,PGVN_DSN=setup.dsn('pgvn_service')),stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        ready(writer)
        setup.run(setup.PROBE,['put',repo['id'].upper(),'writer',txn,'/case-file',payload],role='pgvn_service',expected=1)
        setup.run(setup.PROBE,['commit',repo['id'].upper(),'writer',txn],role='pgvn_service',expected=1)
        out,err=writer.communicate(b'\n',timeout=15);assert writer.returncode==0,err
        result=setup.run(setup.PROBE,['commit',repo['id'].upper(),'writer',txn],role='pgvn_service')
        assert result.stdout.strip()==b'revision=2'
    finally:
        if writer.poll() is None:writer.kill();writer.communicate(timeout=10)
    setup.admin('verify',repo['id'])
    return {'callback_failure_rolls_back_transaction_properties_and_binding':True,'nested_unit_rejected_without_publishing':True,
        'output_unchanged_on_failure':True,'unicode_and_quote_binding':True,'binary_initial_property':True,
        'fresh_process_can_use_and_publish_transaction':True,'concurrent_unique_binding_one_winner_no_orphan':True,
        'process_loss_before_commit_rolls_back_everything':True,'uuid_casing_preserves_writer_exclusion':True,'full_verify':True,
        'scope':'Private native atomic creation foundation. Apache DAV provider, transport hooks and HTTP/HTTPS remain incomplete.'}


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details={};status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 native atomic transaction creation','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-start,'details':details,'error':error,
        'dav_state_probe_sha256':hashlib.sha256(PROBE.read_bytes()).hexdigest(),
        'pgvnserve_sha256':hashlib.sha256((RUNTIME/'bin/pgvnserve.exe').read_bytes()).hexdigest()}
    output=ROOT/'artifacts/pgvn/tests';output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (output/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(output/'m5-creation-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
