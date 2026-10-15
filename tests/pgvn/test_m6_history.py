"""Import an authorized fixed SVN dump into pathless PgVN and an FSFS oracle.

Requires PGVN_TEST_CORPUS_DUMP and PGVN_TEST_CORPUS_SHA256. Never reads or
changes the original Polarion repository. Full verify covers every revision;
logical full-tree/content/property snapshots cover five explicitly named ones.
"""
from pathlib import Path
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME
from test_m4 import build_identity

sys.path.insert(0,str(ROOT/'tests'))
from verify_corpus_properties import dump_revision_properties,property_digest

TEST_ID=setup.TEST_ID.replace('m3-credentials','m6-history')
setup.WORK=RUNTIME/'tests'/TEST_ID
setup.EMPTY=setup.WORK/'pathless-working-directory'
setup.DATABASE='pgvn_m6_history_'+TEST_ID.rsplit('-',1)[1]
DETAILS={}
RESUME_REPORT=None


def sha(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):result.update(block)
    return result.hexdigest()


def command(executable,*args,native=False,input_file=None,output_file=None,timeout=7200):
    arguments=[str(executable),*map(str,args)]
    if native:arguments.append('--json')
    started=time.monotonic()
    incoming=input_file.open('rb') if input_file else None
    outgoing=output_file.open('xb') if output_file else subprocess.PIPE
    try:
        result=subprocess.run(arguments,cwd=setup.EMPTY,env=dict(os.environ,PGVN_DSN=setup.dsn('pgvn_admin')),
            stdin=incoming,stdout=outgoing,stderr=subprocess.PIPE,timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        if incoming:incoming.close()
        if output_file:outgoing.close()
    assert result.returncode==0,(Path(executable).name,result.returncode,result.stderr[:2000])
    return (json.loads(result.stdout) if native else result.stdout),time.monotonic()-started


def admin(*args):return command(setup.ADMIN,*args,native=True)[0]


def checks():
    frozen=build_identity()
    DETAILS.update(build_manifest_sha256=sha(ROOT/'artifacts/pgvn/build/manifest.json'),
        test_source_sha256=sha(Path(__file__)),database=setup.DATABASE,
        work_directory=str(setup.WORK))
    archive=Path(os.environ['PGVN_TEST_CORPUS_DUMP']).resolve()
    expected=os.environ['PGVN_TEST_CORPUS_SHA256'].lower()
    assert len(expected)==64 and sha(archive)==expected,'Fixed export digest mismatch'
    props=dump_revision_properties(archive);head=max(props)
    assert set(props)==set(range(head+1))
    DETAILS.update(archive_sha256=expected,archive_bytes=archive.stat().st_size,head=head,
        original_repository_accessed=False,revision_property_parser_sha256=sha(ROOT/'tests/verify_corpus_properties.py'))
    if RESUME_REPORT:
        previous=json.loads(RESUME_REPORT.read_text(encoding='utf-8'))
        old=previous['details'];work=Path(old['work_directory']).resolve()
        assert work.parent==(RUNTIME/'tests').resolve() and work.name==previous['test_id']
        assert previous['test_id'].startswith('m6-history-') and old['database']=='pgvn_m6_history_'+previous['test_id'].rsplit('-',1)[1]
        assert previous['status']=='FAIL' and old['verified_revisions']==head+1
        assert 'repository or configuration is unavailable' in previous['error']
        assert old['archive_sha256']==expected and old['head']==head
        old_manifest=work/'preparation-manifest.json'
        assert sha(old_manifest)==old['build_manifest_sha256']
        prepared=json.loads(old_manifest.read_text(encoding='utf-8'))
        assert prepared['products']==frozen['products']
        assert prepared['runtime_dependencies']==frozen['runtime_dependencies'] and prepared['apache_dependencies']==frozen['apache_dependencies']
        assert all(frozen['sources'][name]==value for name,value in prepared['sources'].items() if name.startswith(('src/','sql/','web/')))
        assert previous['pgvnadmin_sha256']==sha(setup.ADMIN) and previous['core_probe_sha256']==sha(setup.PROBE)
        setup.WORK=work;setup.EMPTY=work/'pathless-working-directory';setup.DATABASE=old['database']
        setup.fixture.assert_ours()
        repo={'id':str(uuid.UUID(old['repository_id']))}
        DETAILS.update(repository_id=repo['id'],database=setup.DATABASE,work_directory=str(work),
            fsfs_load_seconds=old['fsfs_load_seconds'],native_load_seconds=old['native_load_seconds'],
            preparation_evidence={'test_id':previous['test_id'],'status':previous['status'],'report_sha256':sha(RESUME_REPORT),
                'manifest_sha256':sha(old_manifest),'test_source_sha256':sha(work/'preparation-test-source.py'),
                'original_verify_seconds':old['verify_seconds'],'original_verified_revisions':old['verified_revisions'],
                'note':'Import reused after a fixture access-state failure; full native verify is run again below.'})
    else:
        setup.setup_database();setup.fixture.assert_ours()
    print(json.dumps({'test_id':TEST_ID,'database':setup.DATABASE,'work_directory':str(setup.WORK),
        'head':head,'archive_sha256':expected}),flush=True)
    baseline=setup.WORK/'fsfs-oracle';stock=RUNTIME/'bin';legacy=RUNTIME/'legacy/bin'
    if not RESUME_REPORT:
        command(stock/'svnadmin.exe','create',baseline)
        print('Loading fixed export into the isolated FSFS oracle',flush=True)
        _,elapsed=command(stock/'svnadmin.exe','load',baseline,'--quiet',input_file=archive)
        DETAILS['fsfs_load_seconds']=elapsed
        repo=admin('repo','create','real-history','--route','/m6-history')
        admin('repo','disable',repo['id'])
        print('Loading fixed export into native PgVN',flush=True)
        loaded,elapsed=command(setup.ADMIN,'load',repo['id'],'--file',archive,native=True)
        assert loaded['head']==head
        DETAILS.update(repository_id=repo['id'],native_load_seconds=elapsed)
    assert admin('look','youngest',repo['id'])['youngest']==head
    assert command(stock/'svnlook.exe','youngest',baseline)[0].strip()==str(head).encode()
    stock_uuid=command(stock/'svnlook.exe','uuid',baseline)[0].strip()
    assert setup.run(setup.ADMIN,['look','uuid',repo['id']]).stdout.strip()==stock_uuid
    print('Verifying every native revision from 0 through '+str(head),flush=True)
    verified,elapsed=command(setup.ADMIN,'verify',repo['id'],native=True)
    assert verified['verified_revisions']==head+1
    DETAILS.update(verified_revisions=head+1,verify_seconds=elapsed)
    # Offline import requires a disabled repository. The public core probe uses
    # the normal service admission path, so enable it before logical reads.
    admin('repo','enable',repo['id'])
    validation=setup.WORK/('validation-'+TEST_ID.rsplit('-',1)[1]);validation.mkdir()
    DETAILS['validation_work_directory']=str(validation)
    print('Comparing full logical snapshots at five revisions',flush=True)
    snapshots=[]
    for rev in sorted({0,1,head//2,max(0,head-1),head}):
        expected_snapshot,_=command(legacy/'fs_probe.exe','snapshot',baseline,rev)
        actual,_=command(setup.PROBE,'snapshot',repo['id'],rev)
        assert expected_snapshot.splitlines()==actual.splitlines(),'Snapshot mismatch at r'+str(rev)
        (validation/('r'+str(rev)+'.snapshot')).write_bytes(actual)
        snapshots.append({'revision':rev,'sha256':hashlib.sha256(actual).hexdigest(),
            'nodes':sum(line.startswith(b'N|') for line in actual.splitlines())})
        print('Snapshot r'+str(rev)+' matches',flush=True)
    DETAILS['snapshots']=snapshots
    log,_=command(stock/'svn.exe','log','--xml','--verbose','-r','0:'+str(head),baseline.as_uri(),'--non-interactive')
    entries={int(entry.attrib['revision']):entry for entry in ET.fromstring(log).findall('logentry')}
    # Stock svn log omits r0 even for an explicit 0:HEAD range. Validate its
    # empty changed-path set independently; revision properties still include r0.
    assert set(entries)==set(range(1,head+1))
    assert command(stock/'svnlook.exe','changed',baseline,'-r',0)[0].strip()==b''
    DETAILS['changed_path_oracle']={'r0':'stock svnlook changed, empty',
        'r1_through_head':'stock svn log --xml --verbose'}
    changed_hash=hashlib.sha256();revprop_hash=hashlib.sha256()
    flag_checks=0;unknown_flags=0;kind_checks=0
    print('Comparing changed paths and binary revision properties for every revision',flush=True)
    for rev in range(head+1):
        changes=admin('look','changed',repo['id'],'-r',rev)['changes']
        oracle={} if rev==0 else {p.text:p.attrib for p in entries[rev].findall('paths/path')}
        assert {c['path'] for c in changes}==set(oracle),'Changed path mismatch at r'+str(rev)
        for change in changes:
            other=oracle[change['path']]
            assert change['action']=={'A':'add','D':'delete','M':'modify','R':'replace'}[other['action']]
            assert change['copyfrom_path']==other.get('copyfrom-path')
            copy_rev=other.get('copyfrom-rev')
            assert change['copyfrom_rev']==(int(copy_rev) if copy_rev is not None else -1)
            if other.get('kind') in ('file','dir'):
                assert change['kind']==other['kind'],('Changed node kind',rev,change['path'])
                kind_checks+=1
            for attribute,key in (('text-mods','text_mod'),('prop-mods','prop_mod')):
                if other.get(attribute) in ('true','false'):
                    assert change[key] is (other[attribute]=='true'),('Changed flag',rev,change['path'],key)
                    flag_checks+=1
                else:unknown_flags+=1
        actual_props=admin('look','proplist',repo['id'],'--revprop','--verbose','-r',rev)['properties']
        actual_props={p['name'].encode('utf-8'):base64.b64decode(p['value']) for p in actual_props}
        assert actual_props==props[rev],'Revision property mismatch at r'+str(rev)
        changed_hash.update(json.dumps(changes,sort_keys=True,ensure_ascii=False).encode('utf-8'))
        revprop_hash.update(bytes.fromhex(property_digest(props[rev])))
        if rev%100==0:print('Compared through r'+str(rev),flush=True)
    DETAILS.update(changed_path_revisions_compared=head+1,revision_properties_compared=head+1,
        changed_ledger_sha256=changed_hash.hexdigest(),revprop_ledger_sha256=revprop_hash.hexdigest(),
        changed_node_kinds_compared=kind_checks,known_change_flags_compared=flag_checks,
        oracle_unknown_change_flag_attributes=unknown_flags)
    print('Round-trip native dump back into an isolated FSFS repository',flush=True)
    backdump=validation/'native.dump'
    _,elapsed=command(setup.ADMIN,'dump',repo['id'],'--deltas','--file',backdump)
    DETAILS['native_dump_seconds']=elapsed
    assert dump_revision_properties(backdump)==props
    back=validation/'fsfs-roundtrip';command(stock/'svnadmin.exe','create',back)
    command(stock/'svnadmin.exe','load',back,'--quiet',input_file=backdump)
    for item in snapshots:
        actual,_=command(legacy/'fs_probe.exe','snapshot',back,item['revision'])
        saved=(validation/('r'+str(item['revision'])+'.snapshot')).read_bytes()
        assert actual.splitlines()==saved.splitlines(),'Round-trip snapshot mismatch'
    assert not any(setup.EMPTY.iterdir()),'Native commands created files in the empty working directory'
    DETAILS.update(dump_roundtrip_revision_properties=True,dump_roundtrip_snapshots=True,
        native_dump_sha256=sha(backdump),no_per_repository_directory=True)
    assert build_identity()==frozen,'Frozen source or runtime changed during real-history verification'
    return DETAILS


def main():
    start=time.monotonic()
    try:checks();status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M6 native fixed real history','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-start,
        'details':DETAILS,'error':error,'pgvnadmin_sha256':sha(setup.ADMIN),'core_probe_sha256':sha(setup.PROBE),
        'scope':'Authorized fixed export only. Full native verify and all changed/revision-property records; five logical full snapshots and FSFS dump/load round-trip. No claim of every-node comparison at every revision.'}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m6-history-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({'test_id':TEST_ID,'status':status,'elapsed_seconds':report['elapsed_seconds']}),flush=True)
    return 0 if status=='PASS' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-from',type=Path,help='Reuse the exact isolated import from a recorded post-verify fixture admission failure; all verification is repeated.')
    options=parser.parse_args();RESUME_REPORT=options.resume_from
    raise SystemExit(main())
