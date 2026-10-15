"""Production DAV registry state, ownership, hook visibility and creation recovery."""
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import time
import traceback
import uuid
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME
from test_m5_creation import until,literal
from test_m3_hooks import dead,wait_file

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-registry')
PROBE=RUNTIME/'bin/dav_registry_probe.exe'
HOOKS=setup.WORK/'hooks';EVENTS=setup.WORK/'events'


def invoke(operation,repo,kind,name,user='writer',expected=0):
    result=setup.run(PROBE,[operation,repo['id'],user,kind,name],role='pgvn_service') if expected==0 else setup.run(
        PROBE,[operation,repo['id'],user,kind,name],role='pgvn_service',expected=expected)
    data=json.loads(result.stdout);assert data['ok']==(expected==0),data
    return data


def create(repo,kind,name,user='writer',expected=0):return invoke('create',repo,kind,name,user,expected)
def lookup(repo,kind,name,user='writer',expected=0):return invoke('lookup',repo,kind,name,user,expected)


def publish(repo,hook=None,writer='rw'):
    rules=setup.WORK/('rules-'+uuid.uuid4().hex+'.conf')
    rules.write_text('[/]\nwriter = '+writer+'\nreader = r\n',encoding='utf-8')
    hooks=setup.WORK/('hooks-'+uuid.uuid4().hex+'.json');hooks.write_text(json.dumps({'start-commit':hook} if hook else {}),encoding='utf-8')
    generation=setup.admin('config','show',repo['id'])['generation']
    setup.admin('config','publish',repo['id'],'--expected-generation',generation,'--authz',rules,'--hooks',hooks)


def held_creation(repo,name,label):
    pid=setup.WORK/(label+'.pid');release=setup.WORK/(label+'.release')
    publish(repo,{'executable':'hook_probe.exe','arguments':['gate',str(EVENTS),str(pid),str(release)],'timeout_seconds':15})
    process=subprocess.Popen([str(PROBE),'create',repo['id'],'writer','activity',name],cwd=setup.EMPTY,
        env=dict(os.environ,PGVN_DSN=setup.dsn('pgvn_service'),PGVN_TEST_HOOK_ROOT=str(HOOKS)),
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:wait_file(pid,process)
    except Exception:
        if process.poll() is None:process.kill();process.communicate(timeout=10)
        raise
    txn=setup.sql('SELECT txn_id FROM pgvn.dav_bindings WHERE binding_name='+literal(name))
    assert txn
    return process,int(pid.read_text()),release,txn


def checks():
    setup.setup_database();setup.fixture.assert_ours();HOOKS.mkdir();EVENTS.mkdir()
    shutil.copy2(RUNTIME/'bin/hook_probe.exe',HOOKS/'hook_probe.exe')
    repo=setup.admin('repo','create','DAV registry','--route','/dav-registry')
    other=setup.admin('repo','create','DAV other','--route','/dav-other')
    publish(repo);publish(other)
    assert setup.sql("SELECT has_table_privilege('pgvn_service','pgvn.dav_transactions','INSERT')")=='t'
    assert setup.sql("SELECT has_table_privilege('pgvn_monitor','pgvn.dav_transactions','SELECT')")=='f'
    assert setup.sql("SELECT has_table_privilege('pgvn_monitor','pgvn.dav_bindings','UPDATE')")=='f'
    name="绑定 ' / <opaque>"
    a=create(repo,'activity',name);v=create(repo,'virtual',name);o=create(other,'activity',name)
    ids={a['transaction_id'],v['transaction_id'],o['transaction_id']};assert len(ids)==3
    assert lookup(repo,'activity',name)['transaction_id']==a['transaction_id']
    assert lookup(repo,'virtual',name)['transaction_id']==v['transaction_id']
    assert lookup(other,'activity',name)['transaction_id']==o['transaction_id']
    lookup(repo,'activity',name,user='reader',expected=1)
    invoke('cancel',repo,'activity',name,user='reader',expected=1)
    assert lookup(repo,'activity',name)['phase']=='active'
    before=setup.sql('SELECT count(*) FROM pgfs.transactions')
    create(repo,'activity',name,expected=1)
    create(repo,'activity','reader-cannot-create',user='reader',expected=1)
    create(repo,'activity','anonymous-cannot-create',user='-',expected=1)
    assert setup.sql('SELECT count(*) FROM pgfs.transactions')==before
    assert setup.sql("SELECT encode(value,'hex') FROM pgfs.transaction_properties WHERE txn_id='"+a['transaction_id']+"' AND name='pgvn:binary'")=='6400ff76'
    # Commit on another process. Registry reads the immutable PGFS outcome even
    # without a subsequent HTTP metadata update or a surviving original node.
    payload=setup.WORK/'registry.bin';payload.write_bytes(b'registry\0\xffdata')
    setup.run(setup.PROBE,['put',repo['id'],'writer',a['transaction_id'],'/registry.bin',payload],role='pgvn_service')
    setup.run(setup.PROBE,['commit',repo['id'],'writer',a['transaction_id']],role='pgvn_service')
    committed=lookup(repo,'activity',name);assert committed['phase']=='committed' and committed['committed_revision']==1
    invoke('cancel',repo,'activity',name)
    deleted=lookup(repo,'activity',name);assert deleted['phase']=='deleted' and deleted['committed_revision']==1
    invoke('cancel',repo,'activity',name)
    create(repo,'activity',name,expected=1)
    invoke('cancel',repo,'virtual',name)
    assert lookup(repo,'virtual',name)['phase']=='deleted'
    assert setup.sql("SELECT state FROM pgfs.transactions WHERE txn_id='"+v['transaction_id']+"'")=='aborted'
    # Rejected start hook leaves a tracked failed/aborted intent, not an active
    # transaction that another process could mistake for a successful creation.
    publish(repo,'reject')
    create(repo,'activity','hook-rejected',expected=1)
    assert setup.sql("SELECT d.creation_phase||':'||t.state FROM pgvn.dav_transactions d JOIN pgfs.transactions t USING(repository_id,txn_id) JOIN pgvn.dav_bindings b USING(repository_id,txn_id) WHERE b.binding_name='hook-rejected'")=='failed:aborted'
    processes=[]
    try:
        process,hook_pid,release,txn=held_creation(repo,'visible-during-hook','visibility');processes.append(process)
        state=lookup(repo,'activity','visible-during-hook');assert state['phase']=='creating' and state['creation_pending']
        assert setup.sql("SELECT convert_from(value,'UTF8') FROM pgfs.transaction_properties WHERE txn_id='"+txn+"' AND name='svn:author'")=='writer'
        invoke('recover',dict(repo,id=repo['id'].upper()),'native',txn,expected=1)
        setup.admin('maintenance','recover-dav-creation',repo['id'],txn,expected=7)
        setup.admin('maintenance','recover-dav-creation',repo['id'],txn,role='pgvn_service',expected=5)
        assert setup.sql("SELECT state FROM pgfs.transactions WHERE txn_id='"+txn+"'")=='active'
        assert lookup(repo,'activity','visible-during-hook')['phase']=='creating'
        release.write_text('continue',encoding='ascii');out,err=process.communicate(timeout=20)
        assert process.returncode==0,err;assert json.loads(out)['transaction_id']==txn
        assert lookup(repo,'activity','visible-during-hook')['phase']=='active';dead(hook_pid)
        process,hook_pid,release,txn=held_creation(repo,'revoked-during-hook','revocation');processes.append(process)
        publish(repo,writer='r');release.write_text('continue',encoding='ascii')
        out,err=process.communicate(timeout=20);assert process.returncode==1,(out,err);dead(hook_pid)
        assert setup.sql("SELECT d.creation_phase||':'||t.state FROM pgvn.dav_transactions d JOIN pgfs.transactions t USING(repository_id,txn_id) WHERE d.txn_id='"+txn+"'")=='failed:aborted'
        process,hook_pid,release,txn=held_creation(repo,'creator-lost','process-loss');processes.append(process)
        process.kill();process.communicate(timeout=10);dead(hook_pid)
        until(lambda:setup.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgvn'")=='0')
        recovered=setup.admin('maintenance','recover-dav-creation',repo['id'],txn)
        assert recovered['action']=='aborted_creating' and recovered['state']=='deleted'
        state=lookup(repo,'activity','creator-lost');assert state['phase']=='deleted' and state['creation_failed']
        assert setup.sql("SELECT state FROM pgfs.transactions WHERE txn_id='"+txn+"'")=='aborted'
    finally:
        for process in processes:
            if process.poll() is None:process.kill();process.communicate(timeout=10)
    publish(repo)
    live=create(repo,'native','unused')['transaction_id']
    setup.sql("UPDATE pgvn.dav_transactions SET created_at=clock_timestamp()-interval '2 years' WHERE txn_id='"+live+"'")
    invoke('recover',repo,'native',live)
    assert setup.admin('maintenance','recover-dav-creation',repo['id'],live)['action']=='unchanged'
    assert lookup(repo,'native',live)['phase']=='active'
    # A forged/incomplete schema ledger must not make the new API or db check
    # claim that the necessary tables are present.
    try:
        setup.sql('ALTER TABLE pgvn.dav_bindings RENAME TO dav_bindings_hidden')
        setup.admin('db','check',expected=4)
        lookup(repo,'native',live,expected=1)
    finally:setup.sql('ALTER TABLE pgvn.dav_bindings_hidden RENAME TO dav_bindings')
    assert setup.sql('SELECT count(*) FROM pgvn.dav_bindings b LEFT JOIN pgvn.dav_transactions d USING(repository_id,txn_id) WHERE d.txn_id IS NULL')=='0'
    setup.admin('verify',repo['id']);setup.admin('verify',other['id'])
    return {'schema':6,'service_and_monitor_role_boundaries':True,'repository_kind_and_owner_isolation':True,
        'read_only_identity_cannot_create':True,'duplicate_binding_rolls_back_staging':True,'binary_initial_properties':True,
        'cross_process_commit_result_derived_from_pgfs':True,'delete_retains_commit_receipt_and_tombstone':True,
        'start_hook_rejection_aborts_registered_transaction':True,'committed_staging_visible_while_external_hook_runs':True,
        'live_creation_lease_blocks_recovery_and_abort_is_rolled_back':True,'policy_revocation_during_hook_rejects_activation':True,
        'creator_and_hook_process_loss_recoverable_without_age_guess':True,'old_active_transaction_never_age_deleted':True,
        'missing_schema_table_fails_closed':True,'full_verify':True,
        'scope':'Production registry/core/admin state APIs; HTTP/HTTPS acceptance is recorded separately.'}


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details={};status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 production DAV registry','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-start,'details':details,'error':error,
        'dav_registry_probe_sha256':hashlib.sha256(PROBE.read_bytes()).hexdigest(),
        'pgvnserve_sha256':hashlib.sha256((RUNTIME/'bin/pgvnserve.exe').read_bytes()).hexdigest()}
    output=ROOT/'artifacts/pgvn/tests';output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (output/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(output/'m5-registry-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
