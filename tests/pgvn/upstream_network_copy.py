"""Prepare a real native repository clone for legacy copytree setup only.

The original relocation/copy commands and assertions run against that clone.
No repository alias is materialized and no subprocess output is rewritten.
"""
from pathlib import Path
import hashlib
import json
import re
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def request(config,work,source,destination,repositories,declared,policies,args=(),kwargs=None):
    assert not args and not kwargs,'Unsupported repository copytree options'
    work=Path(work).resolve();source=Path(source).resolve();destination=Path(destination).resolve()
    match=re.fullmatch(r'pgvn_m6_net_([0-9a-f]{12})',config['database']);assert match
    suffix=match[1]
    assert config['backend']=='pgvn' and config['identity_domain']=='upstream-'+suffix
    assert work==(ROOT/'runtime/pgvn/tests'/('m6-network-'+suffix)/'pgvn').resolve() and work.is_dir()
    plan=json.loads((work.parent/'plan.json').read_bytes())
    assert plan['database']==config['database'] and plan['owner']=='pgvn_migrate_'+suffix
    assert plan['endpoint']=='127.0.0.1:16432' and (work.parent/'started.json').is_file()
    assert Path(plan['cluster_data_directory']).resolve()==(ROOT/'runtime/pgvn/pg-primary').resolve()
    assert sha(ROOT/'artifacts/pgvn/build/manifest.json')==config['manifest_sha256']==plan['inputs']['manifest_sha256']
    parent=(work/'svn-test-work/repositories').resolve();assert parent.is_relative_to(work)
    assert source.parent==destination.parent==parent and source!=destination
    assert source in repositories and destination in declared and destination not in repositories
    assert not source.exists() and not destination.exists(),'Native repository alias materialized'
    identifier=repositories[source];assert isinstance(identifier,str) and str(uuid.UUID(identifier))==identifier
    policy=policies[source]
    assert policy['authz']=='[/]\n* = rw\n' and not policy['hooks'] and not policy.get('separate_groups')
    assert policy['settings']=={'identity_domain':config['identity_domain'],'anonymous_access':'read','authenticated_access':'write'}
    route='/'+destination.relative_to(work).as_posix()
    name='upstream-copy-'+hashlib.sha256(route.encode('utf-8')).hexdigest()[:24]
    return {'source_id':identifier,'source_alias':str(source),'destination_alias':str(destination),
            'destination_name':name,'destination_route':route}


def clone(value,admin):
    identifier=value['source_id'];before=admin('repo','show','id:'+identifier)
    assert before['id']==identifier and before['enabled'] and type(before['head']) is int and before['head']>=0
    assert str(uuid.UUID(before['uuid']))==before['uuid']
    result=admin('maintenance','hotcopy','id:'+identifier,value['destination_name'],'--route',value['destination_route'])
    destination=result['id'];assert str(uuid.UUID(destination))==destination and destination!=identifier
    assert result['uuid']==before['uuid']
    after=admin('repo','show','id:'+destination)
    assert after['id']==destination and after['uuid']==before['uuid'] and after['head']==before['head']
    assert after['enabled'] is False and after['routes']==[value['destination_route']]
    verified=admin('verify','id:'+destination)
    assert verified['verified_revisions']==before['head']+1
    assert not Path(value['source_alias']).exists() and not Path(value['destination_alias']).exists()
    return dict(value,destination_id=destination,public_uuid=after['uuid'],head=after['head'],
                verified_revisions=verified['verified_revisions'],operation='production maintenance hotcopy')
