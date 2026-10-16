"""Exercise actual script-helper routing offline with pinned upstream helpers."""
from pathlib import Path
import ast
import codecs
import datetime as dt
import hashlib
import json
import os
import sys
import traceback
import uuid
from test_m6_network import upstream_inputs

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes());entry=ROOT/'tests/pgvn/upstream_network_entry.py'
    for name in ('tests/pgvn/upstream_network_entry.py','tests/pgvn/test_m6_network_scripts.py'):
        assert sha(ROOT/name)==manifest['sources'][name],'Unregistered script-routing input'
    source,archive,lock=upstream_inputs();upstream=source/'subversion/tests/cmdline/svntest/main.py'
    original_tree=ast.parse(upstream.read_text(encoding='utf-8'))
    entry_tree=ast.parse(entry.read_text(encoding='utf-8'))
    entry_main=next(node for node in entry_tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
    original_nodes=[next(node for node in original_tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
                    for name in ('file_write','create_python_hook_script')]
    entry_nodes=[next(node for node in entry_main.body if isinstance(node,ast.FunctionDef) and node.name==name)
                 for name in ('hook_path','hook')]
    test_id='m6-network-scripts-offline-'+uuid.uuid4().hex[:12]
    folder=ROOT/'runtime/pgvn/tests'/test_id;area=folder/'area';area.mkdir(parents=True,exist_ok=False)
    wc=area/'working-copy';wc.mkdir();alias=area/'svn-test-work/repositories/native'
    identifier='11111111-1111-1111-1111-111111111111';registry={alias:identifier};declared={alias}
    policies={alias:{'hooks':{}}};paths={};events=[];calls=[];publications=[];checks=[];status='FAIL';error=None
    original_namespace={'sys':sys,'os':os,'codecs':codecs,'windows':True}
    exec(compile(ast.Module(body=original_nodes,type_ignores=[]),str(upstream),'exec'),original_namespace)
    creator=original_namespace['create_python_hook_script']
    def observed_creator(path,code,alternative=None):
        calls.append((path,code,alternative));return creator(path,code,alternative)
    namespace={'Path':Path,'hashlib':hashlib,'sys':sys,'sha':sha,'work':area,'repositories':registry,
        'declared':declared,'policy':policies,'hook_paths':paths,'full':lambda path:Path(path).resolve(),
        'refresh_registry':lambda:None,'original_hook':observed_creator,
        'event':lambda event,**fields:events.append(dict(event=event,**fields)),
        'publish':lambda repo:publications.append(repo),'bridge':area/'hook-bridge.py','bridge_config':area/'hook-context.json'}
    exec(compile(ast.Module(body=entry_nodes,type_ignores=[]),str(entry),'exec'),namespace)
    hook=namespace['hook'];previous=Path.cwd()
    def reject(operation):
        before=len(calls)
        try:operation()
        except AssertionError:assert len(calls)==before;return
        raise AssertionError('Expected script routing rejection before I/O')
    def check(name,operation):operation();checks.append({'name':name,'status':'PASS'})
    try:
        os.chdir(wc);code='import sys\nfor arg in sys.argv[1:]:\n  print(arg)\n'
        supplied=os.path.join('.','diff');assert hook(supplied,code) is None
        assert calls[-1]==(supplied,code,None) and calls[-1][0] is supplied
        files={path:Path(path).read_bytes() for path in (str((wc/'diff').resolve())+'.py',str((wc/'diff').resolve())+'.bat')}
        for path in files:Path(path).unlink()
        creator(supplied,code)
        assert all(Path(path).read_bytes()==data for path,data in files.items())
        assert events[-1]['event']=='generic-script-helper' and not publications and not alias.exists()
        check('Relative client helper delegates the exact arguments and generates byte-identical original Python/batch files',lambda:None)
        alternative='@echo original batch\n';path=str(wc/'alternative')
        assert hook(path,code,alternative) is None and calls[-1]==(path,code,alternative)
        assert Path(path+'.bat').read_bytes()==alternative.encode('utf-8') and not Path(path+'.py').exists()
        assert events[-1]['cmd_alternative'] is True and not publications
        check('An ordinary Windows cmd_alternative retains original generation without native hook publication',lambda:None)
        raw=b'import sys\nprint("binary")\n';binary_path=str(wc/'binary')
        hook(binary_path,raw)
        assert calls[-1][1] is raw and Path(binary_path+'.py').read_bytes()==raw and not publications
        check('The original helper also retains its accepted UTF-8 byte input',lambda:None)
        outside=folder/'outside';outside.mkdir()
        check('A generic script outside its fixture work directory is rejected before original I/O',lambda:reject(lambda:hook(outside/'probe',code)))
        check('A generic script beneath a native repository alias is rejected before original I/O',lambda:reject(lambda:hook(alias/'probe',code)))
        hook(alias/'hooks/pre-commit',code)
        script=area/'global-hooks'/identifier/'pre-commit.py'
        assert publications==[alias] and script.read_text(encoding='utf-8')==code and not alias.exists()
        policy=policies[alias]['hooks']['pre-commit']
        assert policy['executable']==Path(sys.executable).name and policy['arguments'][-1]==str(script)
        assert policy['arguments'][:2]==['-I','-B'] and policy['timeout_seconds']==30
        check('A registered repository hook remains global, retains its Python body and publishes the bridge policy',lambda:None)
        absolute=script.with_suffix('');replacement='print("replacement")\n'
        hook(absolute,replacement,'@exit 0\n')
        assert publications==[alias,alias] and script.read_text(encoding='utf-8')==replacement
        assert events[-1]['event']=='hook' and events[-1]['cmd_alternative'] is True and not alias.exists()
        check('An already mapped hook retains native bridge behavior including the documented alternative boundary',lambda:None)
        supplied_hashes={str(wc/'diff'):hashlib.sha256(code.encode('utf-8')).hexdigest(),
                        str(wc/'alternative'):hashlib.sha256(code.encode('utf-8')).hexdigest(),
                        binary_path:hashlib.sha256(raw).hexdigest()}
        for event in events:
            if event['event']!='generic-script-helper':continue
            assert event['files'] and event['supplied_python_sha256']==supplied_hashes[event['path']]
            assert all(sha(Path(path))==expected for path,expected in event['files'].items())
        check('Generic helper audit hashes match real files produced by the pinned original helper',lambda:None)
        assert sha(manifest_path)==frozen;status='PASS'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr)
    finally:os.chdir(previous)
    report={'test_id':test_id,'status':status,'component':'M6 script helper routing offline guards',
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
        'build_manifest_sha256':frozen,'entry_source_sha256':sha(entry),'upstream_main_sha256':sha(upstream),
        'source_archive_sha256':sha(archive),'real_subprocesses_started':0,'database_connections_attempted':0,
        'upstream_python_tests_executed':0,'milestone_complete':False}
    target=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');target.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
