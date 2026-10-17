"""Offline historical-WC fixture guards, including a real Windows share lock."""
from pathlib import Path
import ast
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import types
from unittest import mock
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    assert os.name=='nt'
    frozen_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(frozen_path)
    manifest=json.loads(frozen_path.read_bytes());source=ROOT/'tests/pgvn/upstream_network_import.py'
    for name in ('upstream_network_import.py','test_m6_network_upgrade.py'):
        assert sha(ROOT/'tests/pgvn'/name)==manifest['sources']['tests/pgvn/'+name]
    spec=importlib.util.spec_from_file_location('pgvn_upgrade_offline',source)
    boundary=importlib.util.module_from_spec(spec);spec.loader.exec_module(boundary)
    from test_m6_network import upstream_inputs
    vendor,archive,lock=upstream_inputs()
    relative='subversion/tests/cmdline/upgrade_tests.py'
    fixture_relative='subversion/tests/cmdline/upgrade_tests_data/logs_left_1_5.tar.bz2'
    with zipfile.ZipFile(archive) as upstream:
        original_source=upstream.read(lock['ARCHIVE_ROOT']+'/'+relative)
        fixture_bytes=upstream.read(lock['ARCHIVE_ROOT']+'/'+fixture_relative)
    assert hashlib.sha256(original_source).hexdigest()==lock['FILES'][relative]['sha256']
    assert hashlib.sha256(fixture_bytes).hexdigest()==lock['FILES'][fixture_relative]['sha256']
    node=next(node for node in ast.parse(original_source).body
              if isinstance(node,ast.FunctionDef) and node.name=='replace_sbox_with_tarfile')
    helper_code=compile(ast.Module(body=[node],type_ignores=[]),relative,'exec')
    test_id='m6-network-upgrade-offline-'+uuid.uuid4().hex[:12]
    work=ROOT/'runtime/pgvn/tests'/test_id;work.mkdir(parents=True,exist_ok=False)
    fake=work/'fake-root';boundary.ROOT=fake
    snapshot=fake/'runtime/pgvn/upstream-source/original-fixture'
    (snapshot/fixture_relative).parent.mkdir(parents=True)
    (snapshot/fixture_relative).write_bytes(fixture_bytes);(snapshot/relative).write_bytes(original_source)
    marker={'files':{name:lock['FILES'][name] for name in (relative,fixture_relative)}}
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    checks=[];status='FAIL';error=None;counter=0;retry_observations=[]
    saved_cwd=Path.cwd();saved_argv=sys.argv[:];original_rename=os.rename;original_move=shutil.move

    def construct(backend='pgvn',install=True,changes=None):
        nonlocal counter
        counter+=1;suffix=uuid.uuid4().hex[:12]
        area=fake/'runtime/pgvn/tests'/('m6-network-'+suffix)/backend
        for name in ('local_tmp','working_copies'):(area/'svn-test-work'/name).mkdir(parents=True)
        config={'work':str(area),'database':'pgvn_m6_net_'+suffix,'backend':backend}
        config.update(changes or {})
        module=types.ModuleType('unchanged_upgrade_helper_'+str(counter))
        module.__dict__.update(os=os,sys=sys,shutil=shutil,tarfile=tarfile,tempfile=tempfile,
            svntest=types.SimpleNamespace(main=types.SimpleNamespace(
                safe_rmtree=shutil.rmtree,temp_dir='svn-test-work/local_tmp')))
        exec(helper_code,module.__dict__)
        original=module.replace_sbox_with_tarfile;events=[]
        if install:boundary.install_working_copy_moves(module,config,snapshot,marker,
            lambda action,**values:events.append({'event':action,**values}))
        os.chdir(area);sys.argv=[str(snapshot/relative)]
        sandbox=types.SimpleNamespace(wc_dir='svn-test-work/working_copies/upgrade_tests-'+str(counter))
        return module,sandbox,events,original,config

    def reject(operation,exception=(AssertionError,KeyError,OSError)):
        try:operation()
        except exception:return
        raise AssertionError('Expected old-WC fixture boundary rejection')
    def check(name,operation):
        operation();checks.append({'name':name,'status':'PASS'})
    def invoke(module,sandbox):return module.replace_sbox_with_tarfile(sandbox,'logs_left_1_5.tar.bz2')
    def winerror(code):
        error=PermissionError('injected Windows move denial');error.winerror=code;return error
    def share_lock(path):
        handle=kernel.CreateFileW(str(path),0x80000000,1|2,None,3,0x02000000,None)
        assert handle!=wintypes.HANDLE(-1).value,ctypes.get_last_error()
        return handle
    try:
        for backend in ('fsfs','pgvn'):
            module,sandbox,events,original,config=construct(backend)
            invoke(module,sandbox)
            assert len(events)==1 and events[0]['bytes_and_attributes_preserved']
            assert events[0]['attempts']>=1 and sum(events[0]['windows_errors'].values())==events[0]['attempts']-1
            assert Path(sandbox.wc_dir,'.svn/entries').stat().st_file_attributes&1
            assert module.shutil is shutil and shutil.move is original_move
            assert original.__code__.co_code==next(value for value in helper_code.co_consts if isinstance(value,types.CodeType)).co_code
            check(backend+': unchanged extraction and read-only SVN metadata survive the same-volume rename',lambda:None)
        for code in (5,32,33):
            module,sandbox,events,original,config=construct();calls=[]
            def transient(first,second):
                calls.append((first,second))
                if len(calls)<3:raise winerror(code)
                return original_rename(first,second)
            with mock.patch.object(os,'rename',transient),mock.patch.object(shutil,'copytree',side_effect=AssertionError('Copy fallback used')):
                invoke(module,sandbox)
            retry_observations.append({'injected_code':code,'rename_calls':len(calls),'events':events})
            assert events[0]['attempts']==len(calls)>=3 and events[0]['windows_errors'][str(code)]>=2
            assert sum(events[0]['windows_errors'].values())==events[0]['attempts']-1
            assert module.shutil is shutil and shutil.move is original_move
            check('Transient WinError '+str(code)+' retries rename without copying or changing WC attributes',lambda:None)
        for changes,label in (({'database':'pgfs_poc'},'wrong database prefix'),
                              ({'backend':'other'},'unknown backend'),({'work':str(work)},'wrong fixture root')):
            check(label+' rejected before installing an adapter',lambda changes=changes:reject(lambda:construct(changes=changes)))
        for filename,directory,label in (('../logs_left_1_5.tar.bz2',None,'archive path escape'),
                                        ('logs_left_1_5.tar.bz2','../escape','extraction directory escape')):
            module,sandbox,events,original,config=construct()
            check(label+' rejected',lambda:reject(lambda:module.replace_sbox_with_tarfile(sandbox,filename,directory)))
            assert module.shutil is shutil and not events
        module,sandbox,events,original,config=construct()
        (snapshot/fixture_relative).write_bytes(fixture_bytes+b'changed')
        check('Changed locked archive rejected before extraction',lambda:reject(lambda:invoke(module,sandbox)))
        (snapshot/fixture_relative).write_bytes(fixture_bytes)
        for target,label in ((str(work/'outside-working-copies'),'outside WC root'),
                             ('svn-test-work/working_copies','WC root itself')):
            module,sandbox,events,original,config=construct();sandbox.wc_dir=target
            check(label+' rejected before original cleanup',lambda:reject(lambda:invoke(module,sandbox)))
        module,sandbox,events,original,config=construct()
        outside=work/'outside-extraction';outside.mkdir()
        with mock.patch.object(tempfile,'mkdtemp',return_value=str(outside)):
            check('Extraction outside local_tmp cannot be moved',lambda:reject(lambda:invoke(module,sandbox)))
        assert module.shutil is shutil and not events
        module,sandbox,events,original,config=construct()
        destination=Path(sandbox.wc_dir);destination.mkdir();sentinel=destination/'keep';sentinel.write_bytes(b'keep')
        module.svntest.main.safe_rmtree=lambda path:(_ for _ in ()).throw(PermissionError('cleanup denied'))
        check('Failed old-WC cleanup cannot cause a nested move or hide existing data',lambda:reject(lambda:invoke(module,sandbox),FileExistsError))
        assert sentinel.read_bytes()==b'keep' and not events and module.shutil is shutil
        for injected,label in ((winerror(87),'unrecognized permission error'),(OSError('different filesystem error'),'non-permission error')):
            module,sandbox,events,original,config=construct();calls=[]
            def fail(first,second):calls.append(1);raise injected
            with mock.patch.object(os,'rename',fail):
                check(label+' propagates without retry or copy',lambda:reject(lambda:invoke(module,sandbox),OSError))
            assert len(calls)==1 and not events and module.shutil is shutil
        module,sandbox,events,original,config=construct()
        def tamper(first,second):
            original_rename(first,second)
            Path(second,'tampered-file').write_bytes(b'mutated')
        with mock.patch.object(os,'rename',tamper):
            check('Post-move content change fails the archive inventory check',lambda:reject(lambda:invoke(module,sandbox),AssertionError))
        assert not events and module.shutil is shutil
        module,sandbox,events,original,config=construct();handle=[];released=threading.Event();threads=[]
        def delayed_release():
            time.sleep(.2);assert kernel.CloseHandle(handle[0]);released.set()
        def lock_then_rename(first,second):
            if not handle:
                handle.append(share_lock(first));thread=threading.Thread(target=delayed_release)
                threads.append(thread);thread.start()
            return original_rename(first,second)
        try:
            with mock.patch.object(os,'rename',lock_then_rename):invoke(module,sandbox)
        finally:
            for thread in threads:thread.join(timeout=4)
        assert released.is_set() and events[0]['attempts']>1 and events[0]['bytes_and_attributes_preserved']
        check('Real Windows deny-delete directory handle releases and rename succeeds with original bytes/attributes',lambda:None)
        module,sandbox,events,original,config=construct();handle=[]
        def persistent_lock(first,second):
            if not handle:handle.append(share_lock(first))
            return original_rename(first,second)
        began=time.monotonic()
        try:
            with mock.patch.object(os,'rename',persistent_lock):
                check('Persistent real Windows sharing denial remains a failure',lambda:reject(lambda:invoke(module,sandbox),PermissionError))
        finally:
            if handle:assert kernel.CloseHandle(handle[0])
        elapsed=time.monotonic()-began
        assert 3<=elapsed<8 and not Path(sandbox.wc_dir).exists()
        assert events[-1]['event']=='legacy-working-copy-move-failed' and module.shutil is shutil
        check('Retry has a finite deadline, leaves the source, and creates no partial copied WC',lambda:None)
        assert sha(frozen_path)==frozen and shutil.move is original_move and os.rename is original_rename
        status='PASS'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr)
    finally:
        boundary.ROOT=ROOT;os.chdir(saved_cwd);sys.argv=saved_argv
    report={'test_id':test_id,'status':status,'component':'M6 old working-copy fixture offline guards',
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
            'retry_observations':retry_observations,'last_fixture_events':locals().get('events',[]),
            'build_manifest_sha256':frozen,'source_archive_sha256':sha(archive),
            'original_helper_source_sha256':lock['FILES'][relative]['sha256'],
            'original_fixture_sha256':lock['FILES'][fixture_relative]['sha256'],
            'real_subprocesses_started':0,'database_connections_attempted':0,
            'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
