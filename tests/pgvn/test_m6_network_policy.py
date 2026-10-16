"""Offline guards for the upstream invalid-authz file fixture; no real SQL."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import datetime as dt
import hashlib
import json
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';manifest_sha=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes())
    for name in ('tests/pgvn/test_m6_network_policy.py','tests/pgvn/upstream_network_policy.py'):
        assert sha(ROOT/name)==manifest['sources'][name],'Unregistered policy guard input'
    import upstream_network_policy as policy
    test_id='m6-network-policy-offline-'+uuid.uuid4().hex[:12]
    work=ROOT/'runtime/pgvn/tests'/test_id;work.mkdir(parents=True,exist_ok=False)
    checks=[];status='FAIL';error=None
    try:
        original='[repo:/A]\nuser = rw\n[Repo:/A]\n* =\n[/]\n* = r\n# [repo:/B]\n'
        changed,mapping=policy.namespace(original,Path('repo'),'canonical')
        assert changed=='[canonical:/A]\nuser = rw\n[Repo:/A]\n* =\n[/]\n* = r\n# [repo:/B]\n'
        assert mapping['translated_sections']==1
        checks.append({'name':'Exact namespace only; wrong case, global rules and comments retained','status':'PASS'})
        for code,data,expected in ((2,{'error':{'code':'invalid_arguments','message':'Invalid authorization rules'}},True),
                                    (2,{'error':{'code':'invalid_arguments','message':'Unknown setting'}},False),
                                    (2,{'error':[]},False),(1,{},False)):
            assert policy.authz_rejection(SimpleNamespace(returncode=code,stderr=json.dumps(data).encode()))==expected
        checks.append({'name':'Only the specific administrator authz-parser rejection enables raw-file fixture','status':'PASS'})
        tree=work/'fake-root';policy.ROOT=tree
        suffix='123456abcdef';database='pgvn_m6_net_'+suffix;owner='pgvn_migrate_'+suffix
        area=tree/'runtime/pgvn/tests'/('m6-network-'+suffix)/'pgvn';area.mkdir(parents=True)
        frozen=tree/'artifacts/pgvn/build/manifest.json';frozen.parent.mkdir(parents=True);frozen.write_bytes(b'{}')
        plan={'database':database,'owner':owner,'endpoint':'127.0.0.1:16432',
              'inputs':{'manifest_sha256':sha(frozen)}}
        plan_path=area.parent/'plan.json';plan_path.write_text(json.dumps(plan))
        (area.parent/'started.json').write_text('{}')
        config={'backend':'pgvn','database':database}
        current={'id':'11111111-1111-1111-1111-111111111111','generation':1}
        fixture_manifest={'inputs':{'postgres':'SUBPROCESS_MUST_BE_MOCKED'}}
        def invoke(c=config,a=area,r=current):
            return policy.invalid_file(c,fixture_manifest,a,r,'[/]\n* = invalid',None,{},
                                       {'identity_domain':'test','anonymous_access':'none','authenticated_access':'write'})
        def deny(*args,**kwargs):raise RuntimeError('Unexpected subprocess boundary')
        def negative(name,call):
            try:call()
            except (AssertionError,ValueError):pass
            else:raise AssertionError('Guard unexpectedly accepted: '+name)
            checks.append({'name':name,'status':'PASS'})
        with patch.object(policy.subprocess,'run',side_effect=deny) as blocked:
            negative('DDT route outside the ordinary repository mount rejected before subprocess',
                lambda:policy.route_alias(config,fixture_manifest,area,current,area/'outside/repo'))
            negative('DDT route with noncanonical alias spelling rejected before subprocess',
                lambda:policy.route_alias(config,fixture_manifest,area,current,area/'svn-test-work/repositories/bad%20name'))
            negative('DDT route wrong database rejected before subprocess',
                lambda:policy.route_alias(dict(config,database='pgvn_poc'),fixture_manifest,area,current,
                                           area/'svn-test-work/repositories/repo'))
            negative('Wrong database prefix rejected before subprocess',lambda:invoke(c=dict(config,database='pgvn_poc')))
            negative('Wrong runtime directory rejected before subprocess',lambda:invoke(a=area.parent/'outside'))
            plan_path.write_text(json.dumps(dict(plan,owner='pgvn_owner')))
            negative('Wrong database owner in plan rejected before subprocess',invoke)
            plan_path.write_text(json.dumps(plan))
            frozen.write_bytes(b'{"changed":true}')
            negative('Stale frozen manifest rejected before subprocess',invoke)
            frozen.write_bytes(b'{}')
            negative('Non-UUID repository selector rejected before subprocess',lambda:invoke(r=dict(current,id='bad-id')))
            assert blocked.call_count==0
        queried=[]
        def wrong_cluster(arguments,**kwargs):
            queried.append(arguments)
            return SimpleNamespace(returncode=0,stderr='',stdout=json.dumps({
                'directory':str(tree/'wrong-cluster'),'database':database,'owner':owner}))
        with patch.object(policy.subprocess,'run',side_effect=wrong_cluster):
            negative('Wrong actual cluster stops after read-only identity query',invoke)
        assert len(queried)==1 and queried[0][queried[0].index('-U')+1]=='pgvn_owner'
        policy.ROOT=ROOT
        assert sha(manifest_path)==manifest_sha,'Frozen build changed during offline guards'
        status='PASS'
    except BaseException:
        error=traceback.format_exc();print(error,file=sys.stderr)
    report={'test_id':test_id,'status':status,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'component':'M6 upstream authz fixture offline guards','checks':checks,'error':error,
            'build_manifest_sha256':sha(manifest_path),'real_subprocesses_started':0,
            'database_connections_attempted':0,'upstream_python_tests_executed':0}
    path=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');path.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
