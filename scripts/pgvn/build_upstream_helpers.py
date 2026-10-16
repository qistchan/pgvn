"""Build the pinned upstream WC/test helpers in a private runtime.

Stock clients keep their original SDK DLLs. Only the helper directory receives
the original-source WC DLL with extra private exports needed by entries-dump.
No databases, credentials or services are created by this builder.
"""
from pathlib import Path
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from catalog_upstream_network import read_lock

ROOT=Path(__file__).resolve().parents[2]
BUILD=ROOT/'build/pgvn'
OUT=ROOT/'runtime/pgvn/upstream-helpers'
SELF='scripts/pgvn/build_upstream_helpers.py'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'));frozen=sha(manifest_path)
    for name in (SELF,'scripts/pgvn/catalog_upstream_network.py','tests/pgvn/upstream_network_lock.py'):
        assert sha(ROOT/name)==manifest['sources'][name], 'Unregistered or changed helper build input: '+name
    tool_path=ROOT/'artifacts/pgvn/build/upstream-network-tools.json'
    tools=json.loads(tool_path.read_text(encoding='utf-8'))
    assert tools['build_manifest_sha256']==frozen
    for name,expected in tools['fixture_sources'].items():assert sha(ROOT/name)==expected,name
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256'],name
    assert sha(BUILD/'libpgvn_core.a')==manifest['core_archive_sha256']
    inputs=manifest['inputs'];svn=Path(inputs['subversion']);lock=read_lock()
    archive=svn.parent/lock['ARCHIVE_BASENAME']
    assert sha(archive)==lock['ARCHIVE_SHA256'],'Original archive differs from source lock'
    command_sources={n:'subversion/tests/cmdline/'+n+'.c' for n in ('entries-dump','lock-helper','atomic-ra-revprop-change')}
    command_sources.update({n:'subversion/tests/libsvn_wc/'+n+'.c' for n in ('wc-lock-tester','wc-incomplete-tester')})
    wc=svn/'subversion/libsvn_wc'
    wc_sources=sorted(p for p in wc.iterdir() if p.suffix in ('.c','.h','.sql'))
    names={p.relative_to(svn).as_posix() for p in wc_sources}|set(command_sources.values())|{'build/transform_sql.py'}
    source_hashes={}
    # Check all WC C/headers/SQL plus the SQL generator against the pinned ZIP.
    with zipfile.ZipFile(archive) as original:
        archived_wc={n.removeprefix(lock['ARCHIVE_ROOT']+'/') for n in original.namelist()
                     if n.startswith(lock['ARCHIVE_ROOT']+'/subversion/libsvn_wc/')
                     and '/' not in n.removeprefix(lock['ARCHIVE_ROOT']+'/subversion/libsvn_wc/')
                     and Path(n).suffix in ('.c','.h','.sql')}
        assert archived_wc=={p.relative_to(svn).as_posix() for p in wc_sources},'WC source file set differs from archive'
        for name in sorted(names):
            expected=hashlib.sha256(original.read(lock['ARCHIVE_ROOT']+'/'+name)).hexdigest()
            assert sha(svn/name)==expected,'Changed original helper input: '+name
            source_hashes[name]=expected
    upstream_roots={'subversion':svn,'apr':Path(inputs['apr_include']),
                    'apu':Path(inputs['apu_include']),'apache_include':Path(inputs['apache'])/'include'}
    for group,inventory in manifest['upstream'].items():
        for name,expected in inventory.items():assert sha(upstream_roots[group]/name)==expected,(group,name)
    llvm=Path(inputs['toolchain']);compiler=llvm/'clang.exe'
    assert sha(compiler)==manifest['compiler_sha256']
    assert OUT.resolve().is_relative_to((ROOT/'runtime/pgvn').resolve())
    binary=OUT/'bin';generated=OUT/'generated';imports=OUT/'imports'
    for path in (binary,generated,imports):path.mkdir(parents=True,exist_ok=True)
    commands=[]
    def run(command):
        command=list(map(str,command));commands.append(command)
        result=subprocess.run(command,cwd=ROOT,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=240)
        if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace')[-16000:])
        return result.stdout.decode('utf-8','replace')
    def exports(dll):
        output=run([llvm/'llvm-readobj.exe','--coff-exports',dll])
        image=dll.read_bytes();pe=struct.unpack_from('<I',image,0x3c)[0]
        count=struct.unpack_from('<H',image,pe+6)[0];size=struct.unpack_from('<H',image,pe+20)[0]
        sections=[]
        for i in range(count):
            offset=pe+24+size+40*i
            virtual,address,raw=struct.unpack_from('<III',image,offset+8)
            flags=struct.unpack_from('<I',image,offset+36)[0]
            sections.append((address,address+max(virtual,raw),flags))
        definitions=[]
        for block in re.findall(r'Export \{(.*?)\}',output,re.S):
            name=re.search(r'Name: (\S+)',block)[1];address=int(re.search(r'RVA: (0x[0-9A-Fa-f]+)',block)[1],16)
            code=any(start<=address<end and flags&0x20000000 for start,end,flags in sections)
            definitions.append(name+('' if code else ' DATA'))
        assert definitions,'No DLL exports: '+str(dll)
        return definitions
    def import_library(dll):
        definitions=exports(dll);definition=imports/(dll.stem+'.def');library=definition.with_suffix('.a')
        definition.write_text('LIBRARY '+dll.name+'\nEXPORTS\n'+'\n'.join(definitions)+'\n',encoding='ascii')
        run([llvm/'llvm-dlltool.exe','-m','i386:x86-64','-d',definition,'-l',library])
        return library,sorted(d.removesuffix(' DATA') for d in definitions)
    original_dependencies={}
    for name,expected in manifest['runtime_dependencies'].items():
        if name.startswith('bin/') and name.lower().endswith('.dll'):
            source=ROOT/'runtime/pgvn'/name;assert sha(source)==expected
            original_dependencies[source.name]=expected
            if source.name.lower()!='libsvn_wc-1.dll':shutil.copy2(source,binary/source.name)
    libraries={}
    for name in ('libsvn_fs-1','libsvn_fs_util-1','libsvn_client-1','libsvn_ra-1',
                 'libsvn_repos-1','libsvn_diff-1','libsvn_delta-1','libsvn_subr-1','libaprutil-1','libapr-1'):
        libraries[name]=import_library(ROOT/'runtime/pgvn/bin'/(name+'.dll'))[0]
    flags=['-I',str(generated),*[f for f in manifest['flags'] if f not in ('-DNDEBUG','-Werror')]]
    flags+=['-Wno-deprecated-declarations','-Wno-missing-field-initializers']
    run([sys.executable,'-I','-B',svn/'build/transform_sql.py',wc/'wc-queries.sql',generated/'wc-queries.h'])
    rebuilt=binary/'libsvn_wc-1.dll'
    wc_imports=[libraries[n] for n in ('libsvn_subr-1','libsvn_delta-1','libsvn_diff-1','libaprutil-1','libapr-1')]
    run([compiler,*flags,'-shared','-Wl,--export-all-symbols','-Wl,--no-insert-timestamp',
         *sorted(wc.glob('*.c')),*wc_imports,'-o',rebuilt])
    libraries['libsvn_wc-1'],actual=import_library(rebuilt)
    sdk_exports=[n.removesuffix(' DATA') for n in exports(ROOT/'runtime/pgvn/bin/libsvn_wc-1.dll')]
    assert set(sdk_exports)<=set(actual),'Rebuilt helper WC DLL is missing original SDK exports'
    products={};links=[*libraries.values()];resource=BUILD/'objects/pgvn-utf8.o'
    fixture=ROOT/'runtime/pgvn/upstream-tools/objects/network-fixture.o'
    fixture_hash=sha(fixture);core=BUILD/'libpgvn_core.a';core_hash=sha(core)
    for name,relative in command_sources.items():
        target=binary/(name+'.exe');source=svn/relative
        native=[];redirect=[]
        if name=='lock-helper':
            native=[fixture,core,Path(inputs['postgres'])/'lib/libpq.lib','-lbcrypt','-ladvapi32']
            redirect=['-include',str(ROOT/'tests/pgvn/upstream_network_redirect.inc')]
        run([compiler,*flags,*redirect,source,resource,*native,*links,'-Wl,--no-insert-timestamp','-o',target])
        products[name]={'path':target.relative_to(ROOT).as_posix(),'sha256':sha(target),'bytes':target.stat().st_size}
        if name=='lock-helper':
            stock=binary/'lock-helper-fsfs.exe'
            run([compiler,*flags,source,resource,*links,'-Wl,--no-insert-timestamp','-o',stock])
            products['lock-helper-fsfs']={'path':stock.relative_to(ROOT).as_posix(),'sha256':sha(stock),'bytes':stock.stat().st_size}
    for name,expected in source_hashes.items():assert sha(svn/name)==expected,'Source changed during helper build: '+name
    assert sha(fixture)==fixture_hash and sha(core)==core_hash
    for name,expected in original_dependencies.items():
        assert sha(ROOT/'runtime/pgvn/bin'/name)==expected,'Stock client runtime changed: '+name
        if name.lower()!='libsvn_wc-1.dll':assert sha(binary/name)==expected
    assert sha(manifest_path)==frozen
    report={'status':'BUILT_NOT_PROTOCOL_ACCEPTED','build_manifest_sha256':frozen,
            'upstream_network_tools_manifest_sha256':sha(tool_path),'builder_sha256':sha(ROOT/SELF),
            'archive_sha256':lock['ARCHIVE_SHA256'],'source_hashes':source_hashes,
            'compiler_sha256':sha(compiler),'generated_sql_header_sha256':sha(generated/'wc-queries.h'),
            'helper_wc':{'path':rebuilt.relative_to(ROOT).as_posix(),'sha256':sha(rebuilt),
                         'original_exports':len(sdk_exports),'rebuilt_exports':len(actual),
                         'extra_exports':sorted(set(actual)-set(sdk_exports))},
            'fixture_object_sha256':fixture_hash,'core_archive_sha256':core_hash,
            'products':products,'stock_runtime_unchanged':True,
            'runtime_dependencies':{p.relative_to(ROOT).as_posix():sha(p) for p in sorted(binary.glob('*.dll'))},
            'commands':commands,'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/build/upstream-helpers.json'
    path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','helper_wc','products','stock_runtime_unchanged','upstream_python_tests_executed')}))
    return 0

if __name__=='__main__':raise SystemExit(main())
