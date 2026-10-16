"""Offline guards for native copytree fixture preparation; no SQL/processes."""
from pathlib import Path
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes());source=ROOT/'tests/pgvn/upstream_network_copy.py'
    for name in ('tests/pgvn/upstream_network_copy.py','tests/pgvn/test_m6_network_copy.py'):
        assert sha(ROOT/name)==manifest['sources'][name],'Unregistered copy fixture input'
    spec=importlib.util.spec_from_file_location('pgvn_copy_offline',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    test_id='m6-network-copy-offline-'+uuid.uuid4().hex[:12]
    work=ROOT/'runtime/pgvn/tests'/test_id;work.mkdir(parents=True,exist_ok=False)
    fake=work/'fake-root';module.ROOT=fake
    area=fake/'runtime/pgvn/tests/m6-network-123456abcdef/pgvn';area.mkdir(parents=True)
    first=area/'svn-test-work/repositories/source';second=first.with_name('source.relocated')
    identifier='11111111-1111-1111-1111-111111111111'
    registered={first:identifier};declared={first,second}
    policies={first:{'authz':'[/]\n* = rw\n','hooks':{},'settings':{
        'identity_domain':'upstream-123456abcdef','anonymous_access':'read','authenticated_access':'write'}}}
    fake_manifest=fake/'artifacts/pgvn/build/manifest.json';fake_manifest.parent.mkdir(parents=True)
    fake_manifest.write_text('{"offline_scope":true}\n',encoding='utf-8')
    config={'database':'pgvn_m6_net_123456abcdef','backend':'pgvn',
            'identity_domain':'upstream-123456abcdef','manifest_sha256':sha(fake_manifest)}
    plan={'database':config['database'],'owner':'pgvn_migrate_123456abcdef','endpoint':'127.0.0.1:16432',
          'cluster_data_directory':str(fake/'runtime/pgvn/pg-primary'),'inputs':{'manifest_sha256':sha(fake_manifest)}}
    plan_path=area.parent/'plan.json';plan_path.write_text(json.dumps(plan));(area.parent/'started.json').write_text('{}')
    checks=[];status='FAIL';error=None
    def request(**changes):
        values=dict(config=config,work=area,source=first,destination=second,repositories=registered,
                    declared=declared,policies=policies);values.update(changes)
        return module.request(**values)
    def reject(operation):
        try:operation()
        except (AssertionError,ValueError):return
        raise AssertionError('Expected copy boundary rejection')
    def check(name,operation):operation();checks.append({'name':name,'status':'PASS'})
    try:
        value=request();assert value['source_id']==identifier and value['destination_route']=='/svn-test-work/repositories/source.relocated'
        assert not first.exists() and not second.exists()
        check('Valid registered source and declared target prepare a route without disk aliases',lambda:None)
        check('Wrong database prefix rejected',lambda:reject(lambda:request(config=dict(config,database='pgfs_poc'))))
        check('Wrong fixture root rejected',lambda:reject(lambda:request(work=work)))
        plan['owner']='other_owner';plan_path.write_text(json.dumps(plan))
        check('Wrong plan owner rejected',lambda:reject(request));plan['owner']='pgvn_migrate_123456abcdef'
        plan['endpoint']='127.0.0.1:5432';plan_path.write_text(json.dumps(plan))
        check('Installed-cluster endpoint rejected',lambda:reject(request));plan['endpoint']='127.0.0.1:16432';plan_path.write_text(json.dumps(plan))
        check('Stale frozen manifest rejected',lambda:reject(lambda:request(config=dict(config,manifest_sha256='bad'))))
        check('Undeclared copy target rejected',lambda:reject(lambda:request(declared={first})))
        check('Target outside repository mount rejected',lambda:reject(lambda:request(destination=area/'outside',declared={area/'outside'})))
        check('Already registered destination rejected',lambda:reject(lambda:request(repositories={first:identifier,second:identifier})))
        check('Malformed source UUID rejected',lambda:reject(lambda:request(repositories={first:'not-a-uuid'})))
        second.mkdir(parents=True)
        check('Materialized destination rejected',lambda:reject(request));second.rmdir()
        policies[first]['hooks']['pre-commit']='allow'
        check('Non-default hook policy rejected',lambda:reject(request));policies[first]['hooks'].clear()
        check('Non-default copytree options rejected',lambda:reject(lambda:request(kwargs={'dirs_exist_ok':True})))
        assert sha(manifest_path)==frozen
        status='PASS'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr)
    finally:module.ROOT=ROOT
    report={'test_id':test_id,'status':status,'component':'M6 native copy fixture offline guards',
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
        'build_manifest_sha256':frozen,'real_subprocesses_started':0,'database_connections_attempted':0,
        'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');path.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
