"""Link unchanged pinned Apache FS/repos tests to PgVN's directory-free core."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[2]
BUILD=ROOT/'build/pgvn'
OUT=ROOT/'runtime/pgvn/native-tests'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    archive=BUILD/'libpgvn_core.a'
    assert sha(archive)==manifest['core_archive_sha256'],'Core archive differs from frozen product build'
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256'],name
    for name in ('tests/pgvn/upstream_native_fixture.c','tests/pgvn/upstream_native_redirect.inc','tests/pgvn/native_path_probe.c','tests/pgvn/native_applicability.py','scripts/pgvn/build_native_tests.py'):
        assert sha(ROOT/name)==manifest['sources'][name],name
    inputs=manifest['inputs'];vendor=Path(inputs['subversion']);svn=vendor/'subversion';tools=Path(inputs['toolchain'])
    OUT.mkdir(parents=True,exist_ok=True);obj=OUT/'objects';obj.mkdir(exist_ok=True);binary=OUT/'bin';binary.mkdir(exist_ok=True)
    source_names=('svn_test_main.c','svn_test_fs.c','svn_test.h','svn_test_fs.h','libsvn_fs/fs-test.c','libsvn_repos/repos-test.c','libsvn_repos/dir-delta-editor.c')
    sources={}
    for name in source_names:
        path=svn/'tests'/name;expected=manifest['upstream']['subversion'][path.relative_to(vendor).as_posix()]
        assert sha(path)==expected,name;sources[name]=expected
    options=list(manifest['flags'])
    options=[o for o in options if o not in ('-Werror','-DNDEBUG')]+['-Wno-deprecated-declarations','-Wno-missing-field-initializers','-I',str(svn/'tests')]
    compiler=tools/'clang.exe'
    assert sha(compiler)==manifest['compiler_sha256'],'Compiler changed after product build'
    commands=[]
    def run(args):
        args=list(map(str,args));commands.append(args)
        result=subprocess.run(args,cwd=ROOT,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=120)
        if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace')[-12000:])
    libraries=[BUILD/'imports'/(name+'.a') for name in ('libsvn_fs-1','libsvn_fs_util-1','libsvn_diff-1','libsvn_delta-1','libsvn_subr-1','libaprutil-1','libapr-1')]
    links=[archive,*libraries,Path(inputs['postgres'])/'lib/libpq.lib','-lbcrypt','-ladvapi32']
    common=[]
    fixture_support=(svn/'tests/svn_test_fs.c').read_text(encoding='utf-8')
    renames={}
    for symbol in ('svn_test__create_fs','svn_test__create_fs2','svn_test__create_repos','svn_test__create_repos2'):
        old='\n'+symbol+'(';new='\npgvn_unused_'+symbol+'('
        assert fixture_support.count(old)==1,symbol
        fixture_support=fixture_support.replace(old,new,1);renames[old]=new
    generated=OUT/'generated';generated.mkdir(exist_ok=True)
    generated_support=generated/'svn_test_fs_fixture.c';generated_support.write_text(fixture_support,encoding='utf-8')
    for name in ('svn_test_main.c','svn_test_fs.c'):
        target=obj/(Path(name).stem+'.o');extra=[]
        selected_source=generated_support if name=='svn_test_fs.c' else svn/'tests'/name
        run([compiler,*options,*extra,'-c',selected_source,'-o',target]);common.append(target)
    fixture=obj/'pgvn-fixture.o'
    run([compiler,*options,'-c',ROOT/'tests/pgvn/upstream_native_fixture.c','-o',fixture]);common.append(fixture)
    resource=BUILD/'objects/pgvn-utf8.o'
    products={}
    case_names={}
    for name in ('fs','repos'):
        test=svn/'tests'/('libsvn_'+name)/(name+'-test.c');target=binary/('pgvn-'+name+'-test.exe')
        table=test.read_text(encoding='utf-8').split('static struct svn_test_descriptor_t test_funcs[] =',1)[1]
        case_names[name]=re.findall(r'SVN_TEST_(?:OPTS_PASS|PASS2|SKIP2)\(\s*(\w+)\s*,',table)
        test_obj=obj/(name+'-test.o')
        run([compiler,*options,'-include',ROOT/'tests/pgvn/upstream_native_redirect.inc','-c',test,'-o',test_obj])
        extra=[svn/'tests/libsvn_repos/dir-delta-editor.c'] if name=='repos' else []
        run([compiler,*options,test_obj,*extra,*common,resource,*links,'-Wl,--no-insert-timestamp','-o',target])
        products[name]={'path':str(target.relative_to(ROOT)),'sha256':sha(target),'source_sha256':sha(test)}
    original_support=obj/'svn_test_fs_original.o'
    run([compiler,*options,'-c',svn/'tests/svn_test_fs.c','-o',original_support])
    for name in ('fs','repos'):
        test=svn/'tests'/('libsvn_'+name)/(name+'-test.c');target=binary/('fsfs-'+name+'-test.exe')
        test_obj=obj/(name+'-control.o')
        run([compiler,*options,'-c',test,'-o',test_obj])
        extra=[svn/'tests/libsvn_repos/dir-delta-editor.c'] if name=='repos' else []
        run([compiler,*options,test_obj,*extra,obj/'svn_test_main.o',original_support,resource,
            BUILD/'imports/libsvn_repos-1.a',*libraries,'-Wl,--no-insert-timestamp','-o',target])
        products['fsfs-'+name]={'path':str(target.relative_to(ROOT)),'sha256':sha(target),'source_sha256':sha(test)}
    probe=ROOT/'tests/pgvn/native_path_probe.c';target=binary/'pgvn-native-path-probe.exe'
    run([compiler,*options,probe,*common,resource,*links,'-Wl,--no-insert-timestamp','-o',target])
    products['path-probe']={'path':str(target.relative_to(ROOT)),'sha256':sha(target),'source_sha256':sha(probe)}
    target=binary/'directory-fsfs-repos-test.exe'
    run([compiler,*options,obj/'repos-control.o',svn/'tests/libsvn_repos/dir-delta-editor.c',
        obj/'svn_test_main.o',original_support,resource,*links,'-Wl,--no-insert-timestamp','-o',target])
    products['directory-fsfs-repos']={'path':str(target.relative_to(ROOT)),'sha256':sha(target),
        'source_sha256':sha(svn/'tests/libsvn_repos/repos-test.c')}
    runtime_dependencies={}
    for name,expected in manifest['runtime_dependencies'].items():
        if name.lower().endswith('.dll'):
            source=ROOT/'runtime/pgvn'/name;assert sha(source)==expected;shutil.copyfile(source,binary/source.name)
            runtime_dependencies[(binary/source.name).relative_to(ROOT).as_posix()]=expected
    assert {p.name for p in binary.glob('*.dll')}=={Path(name).name for name in runtime_dependencies},'Unexpected native-test DLLs'
    report={'build_manifest_sha256':sha(manifest_path),'core_archive_sha256':sha(archive),'sources':sources,'products':products,'commands':commands,
        'case_names':case_names,
        'runtime_dependencies':runtime_dependencies,
        'link_inputs':{str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p):sha(p) for p in [archive,*libraries,Path(inputs['postgres'])/'lib/libpq.lib',resource]},
        'support_fixture':{'generated_sha256':sha(generated_support),'definition_renames':renames,'assertions_unchanged':True},
        'fixture_sources':{n:sha(ROOT/n) for n in ('tests/pgvn/upstream_native_fixture.c','tests/pgvn/upstream_native_redirect.inc')},
        'scope':'Upstream test case sources unchanged and test assertions enabled. The copied support file renames four constructor definitions only; all helper calls bind to PgVN constructors. Path reopen calls are fixture-routed. Native bindings use administrative context; network policy is separately tested.'}
    (ROOT/'artifacts/pgvn/build/native-tests.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'products':products,'core_archive_sha256':report['core_archive_sha256']}))
    return 0

if __name__=='__main__':raise SystemExit(main())
