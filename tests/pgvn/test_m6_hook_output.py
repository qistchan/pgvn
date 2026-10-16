"""Offline boundary checks for legacy hook scratch files; no SQL/processes."""
from pathlib import Path
import builtins
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes());source=ROOT/'tests/pgvn/upstream_hook_output.py'
    for name in ('tests/pgvn/upstream_hook_output.py','tests/pgvn/test_m6_hook_output.py'):
        assert sha(ROOT/name)==manifest['sources'][name],'Unregistered hook-output input'
    spec=importlib.util.spec_from_file_location('pgvn_hook_output_offline',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    test_id='m6-hook-output-offline-'+uuid.uuid4().hex[:12]
    work=ROOT/'runtime/pgvn/tests'/test_id;work.mkdir(parents=True,exist_ok=False)
    fake=work/'fake-root';module.ROOT=fake
    area=fake/'runtime/pgvn/tests/m6-network-123456abcdef/pgvn';area.mkdir(parents=True)
    alias=area/'svn-test-work/repositories/repo';identifier='11111111-1111-1111-1111-111111111111'
    registry={alias:identifier};checks=[];status='FAIL';error=None
    original=(builtins.open,os.path.exists,os.unlink);restore=None;saved_argv=sys.argv[:]
    def check(name,operation):
        operation();checks.append({'name':name,'status':'PASS'})
    def reject(operation,exception=(AssertionError,ValueError)):
        try:operation()
        except exception:return
        raise AssertionError('Expected boundary rejection')
    try:
        check('Wrong fixture root rejected before replacing I/O functions',lambda:reject(lambda:module.install(work,lambda:registry)))
        restore=module.install(area,lambda:registry)
        output=alias/'hooks.log'
        with open(output,'w',encoding='utf-8') as stream:stream.write('hook\n')
        assert os.path.exists(output) and not alias.exists()
        with open(output,encoding='utf-8') as stream:assert stream.read()=='hook\n'
        check('Registered hooks.log round-trips through global output without creating an alias',lambda:None)
        check('Unknown repository is not redirected',lambda:reject(lambda:open(area/'svn-test-work/repositories/unknown/hooks.log','w'),FileNotFoundError))
        check('Other repository filenames remain real filesystem failures',lambda:reject(lambda:open(alias/'format','w'),FileNotFoundError))
        os.unlink(output);assert not os.path.exists(output)
        check('Unlink removes only the relocated file',lambda:None)
        registry[alias]='invalid-id'
        check('Malformed repository ID rejected',lambda:reject(lambda:os.path.exists(output)))
        registry[alias]=identifier;alias.mkdir(parents=True)
        check('Materialized native alias rejected instead of hidden by a redirect',lambda:reject(lambda:os.path.exists(output)))
        alias.rmdir()
        restore();restore=None;assert (builtins.open,os.path.exists,os.unlink)==original
        check('Original I/O functions restored',lambda:None)
        target=area/'global-hooks'/identifier/'probe.py';target.parent.mkdir(parents=True)
        target.write_text("import sys\nwith open(sys.argv[1]+'/hooks.log','a') as stream: stream.write('unchanged body\\n')\n",encoding='utf-8')
        fake_manifest=fake/'artifacts/pgvn/build/manifest.json';fake_manifest.parent.mkdir(parents=True)
        fake_manifest.write_text(json.dumps({'sources':{'tests/pgvn/upstream_hook_output.py':sha(source)}}),encoding='utf-8')
        config={'database':'pgvn_m6_net_123456abcdef','work':str(area),'manifest_sha256':sha(fake_manifest),
                'hook_output_sha256':sha(source),'repositories':{identifier:str(alias)}}
        context=area/'hook-context.json';context.write_text(json.dumps(config),encoding='utf-8')
        sys.argv=[str(source),str(context),str(target),str(alias)];assert module.main()==0
        assert (area/'global-hook-output'/identifier/'hooks.log').read_text()=='unchanged body\n' and not alias.exists()
        check('Runner executes hook body unchanged with original alias argv',lambda:None)
        assert (builtins.open,os.path.exists,os.unlink)==original
        config['hook_output_sha256']='bad';context.write_text(json.dumps(config),encoding='utf-8')
        sys.argv=[str(source),str(context),str(target),str(alias)]
        check('Tampered runner hash rejected before hook execution',lambda:reject(module.main))
        assert sha(manifest_path)==frozen
        status='PASS'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr)
    finally:
        if restore:restore()
        sys.argv=saved_argv;module.ROOT=ROOT
        assert (builtins.open,os.path.exists,os.unlink)==original
    report={'test_id':test_id,'status':status,'component':'M6 hook-output fixture offline guards',
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
            'build_manifest_sha256':frozen,'real_subprocesses_started':0,'database_connections_attempted':0,
            'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');path.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
