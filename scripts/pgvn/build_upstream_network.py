"""Build original svnadmin/svnlook command bodies against a guarded PgVN fixture.

No database/service/credential actions occur here. These are test-only tools;
the production pgvnadmin contract and the pinned upstream sources stay unchanged.
"""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from catalog_upstream_network import read_lock

ROOT=Path(__file__).resolve().parents[2]
BUILD=ROOT/'build/pgvn'
OUT=ROOT/'runtime/pgvn/upstream-tools'
REPORT=ROOT/'artifacts/pgvn/build/upstream-network-tools.json'
OWN_INPUTS=('scripts/pgvn/build_upstream_network.py','tests/pgvn/upstream_network_cli.c',
            'tests/pgvn/upstream_network_redirect.inc')
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    frozen_manifest=sha(manifest_path)
    for name in OWN_INPUTS:
        assert name in manifest['sources'] and sha(ROOT/name)==manifest['sources'][name], 'Unregistered or changed test-tool input: '+name
    for name in ('scripts/pgvn/catalog_upstream_network.py','tests/pgvn/upstream_network_lock.py'):
        assert sha(ROOT/name)==manifest['sources'][name], 'Changed source-provenance guard: '+name
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256'],name
    archive=BUILD/'libpgvn_core.a'
    assert sha(archive)==manifest['core_archive_sha256'],'Core archive changed after product build'
    inputs=manifest['inputs'];svn_root=Path(inputs['subversion'])
    lock=read_lock();source_archive=svn_root.parent/lock['ARCHIVE_BASENAME']
    assert sha(source_archive)==lock['ARCHIVE_SHA256'],'Original source archive differs from committed lock'
    command_sources={}
    with zipfile.ZipFile(source_archive) as original:
        for command in ('svnadmin','svnlook'):
            name='subversion/'+command+'/'+command+'.c'
            data=original.read(lock['ARCHIVE_ROOT']+'/'+name)
            expected=hashlib.sha256(data).hexdigest()
            assert sha(svn_root/name)==expected,'Command body differs from original archive: '+name
            command_sources[name]=expected
    upstream_roots={'subversion':svn_root,'apr':Path(inputs['apr_include']),
                    'apu':Path(inputs['apu_include']),'apache_include':Path(inputs['apache'])/'include'}
    for group,inventory in manifest['upstream'].items():
        assert group in upstream_roots, 'Unknown pinned input group: '+group
        for name,expected in inventory.items():assert sha(upstream_roots[group]/name)==expected,(group,name)
    compiler=Path(inputs['toolchain'])/'clang.exe'
    assert sha(compiler)==manifest['compiler_sha256'],'Compiler changed after product build'
    assert OUT.resolve().is_relative_to((ROOT/'runtime/pgvn').resolve())
    OUT.mkdir(parents=True,exist_ok=True)
    obj=OUT/'objects';obj.mkdir(exist_ok=True)
    binary=OUT/'bin';binary.mkdir(exist_ok=True)
    flags=[f for f in manifest['flags'] if f not in ('-DNDEBUG','-Werror')]
    flags+=['-Wno-deprecated-declarations','-Wno-missing-field-initializers']
    own_flags=[*flags,'-Werror']
    commands=[]
    def run(command):
        command=list(map(str,command));commands.append(command)
        result=subprocess.run(command,cwd=ROOT,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8','replace')[-16000:])
    fixture=obj/'network-fixture.o'
    run([compiler,*own_flags,'-c',ROOT/'tests/pgvn/upstream_network_cli.c','-o',fixture])
    imports=[BUILD/'imports'/(n+'.a') for n in ('libsvn_fs-1','libsvn_fs_util-1','libsvn_diff-1','libsvn_delta-1','libsvn_subr-1','libaprutil-1','libapr-1')]
    resource=BUILD/'objects/pgvn-utf8.o'
    link_inputs=[archive,*imports,Path(inputs['postgres'])/'lib/libpq.lib',resource]
    before_links={str(p):sha(p) for p in link_inputs}
    products={};source_records={}
    redirect=ROOT/'tests/pgvn/upstream_network_redirect.inc'
    for command in ('svnadmin','svnlook'):
        source=svn_root/'subversion'/command/(command+'.c')
        expected=manifest['upstream']['subversion'][source.relative_to(svn_root).as_posix()]
        assert sha(source)==expected==command_sources[source.relative_to(svn_root).as_posix()]
        source_records[source.relative_to(svn_root).as_posix()]=expected
        compiled=obj/(command+'.o')
        run([compiler,*flags,'-include',redirect,'-c',source,'-o',compiled])
        target=binary/('pgvn-upstream-'+command+'.exe')
        run([compiler,*flags,compiled,fixture,resource,archive,*imports,Path(inputs['postgres'])/'lib/libpq.lib',
             '-lbcrypt','-ladvapi32','-Wl,--no-insert-timestamp','-o',target])
        products[command]={'path':target.relative_to(ROOT).as_posix(),'sha256':sha(target),'bytes':target.stat().st_size}
    dependencies={}
    for name,expected in manifest['runtime_dependencies'].items():
        if name.startswith('bin/') and name.lower().endswith('.dll'):
            source=ROOT/'runtime/pgvn'/name;assert sha(source)==expected
            destination=binary/source.name;shutil.copy2(source,destination)
            assert sha(destination)==expected
            dependencies[destination.relative_to(ROOT).as_posix()]=expected
    assert {p.name for p in binary.glob('*.dll')}=={Path(n).name for n in dependencies},'Unexpected test-tool DLLs'
    assert {str(p):sha(p) for p in link_inputs}==before_links,'Link inputs changed during compilation'
    assert sha(manifest_path)==frozen_manifest,'Product manifest changed during test-tool compilation'
    for name in OWN_INPUTS:assert sha(ROOT/name)==manifest['sources'][name]
    for name,expected in source_records.items():assert sha(svn_root/name)==expected
    report={'status':'BUILT_NOT_PROTOCOL_ACCEPTED','build_manifest_sha256':frozen_manifest,
            'products':products,'runtime_dependencies':dependencies,'command_sources':source_records,
            'fixture_sources':{n:sha(ROOT/n) for n in OWN_INPUTS},'compiler_sha256':sha(compiler),
            'source_provenance':{'archive_sha256':lock['ARCHIVE_SHA256'],
                                 'lock_sha256':sha(ROOT/'tests/pgvn/upstream_network_lock.py'),
                                 'command_members_verified':True},
            'core_archive_sha256':sha(archive),'link_inputs':before_links,'commands':commands,
            'assertions_enabled':True,'upstream_command_bodies_unchanged':True,'tests_executed':0,
            'milestone_complete':False,
            'scope':'Test-only original SVN command frontends with guarded ID-backed constructor/maintenance bindings; no Python suite or network tests executed by this builder.',
            'pending':['Actual fixture/CLI behavior, original assertions over SVN/HTTP/HTTPS, FSFS controls and case applicability.',
                       'Recover lock-wait and notification semantics; PgVN schema migration has no per-repository on-disk upgrade.']}
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    REPORT.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'products':products,'tests_executed':0}))
    return 0

if __name__=='__main__':raise SystemExit(main())
