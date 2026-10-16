"""Build unchanged upstream mod_dontdothat for private FSFS/PgVN test hosts.

Only the PgVN variant force-includes the existing DAV symbol namespace. This
does not install a module or modify any server configuration or database.
"""
from pathlib import Path
import hashlib
import json
import re
import struct
import subprocess
import zipfile
from catalog_upstream_network import read_lock

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'runtime/pgvn/upstream-http'
SELF='scripts/pgvn/build_upstream_http.py'
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes())
    for name in (SELF,'scripts/pgvn/catalog_upstream_network.py','tests/pgvn/upstream_network_lock.py'):
        assert sha(ROOT/name)==manifest['sources'][name],name
    inputs=manifest['inputs'];svn=Path(inputs['subversion']);lock=read_lock()
    archive=svn.parent/lock['ARCHIVE_BASENAME'];assert sha(archive)==lock['ARCHIVE_SHA256']
    relative='tools/server-side/mod_dontdothat/mod_dontdothat.c';source=svn/relative
    with zipfile.ZipFile(archive) as original:
        expected=hashlib.sha256(original.read(lock['ARCHIVE_ROOT']+'/'+relative)).hexdigest()
    assert sha(source)==expected,'Original mod_dontdothat source changed'
    roots={'subversion':svn,'apr':Path(inputs['apr_include']),'apu':Path(inputs['apu_include']),
           'apache_include':Path(inputs['apache'])/'include'}
    for group,inventory in manifest['upstream'].items():
        for name,value in inventory.items():assert sha(roots[group]/name)==value,(group,name)
    namespace=ROOT/'build/pgvn/upstream/dav/pgvn_dav_namespace.h'
    assert sha(namespace)==manifest['dav_patches']['namespace_sha256']
    llvm=Path(inputs['toolchain']);compiler=llvm/'clang.exe';assert sha(compiler)==manifest['compiler_sha256']
    imports=OUT/'imports';imports.mkdir(parents=True,exist_ok=True);commands=[]
    def run(command):
        command=list(map(str,command));commands.append(command)
        result=subprocess.run(command,cwd=ROOT,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=90)
        assert result.returncode==0,result.stderr.decode('utf-8','replace')[-10000:]
        return result.stdout.decode('utf-8','replace')
    def import_library(dll,only=None):
        output=run([llvm/'llvm-readobj.exe','--coff-exports',dll])
        image=dll.read_bytes();pe=struct.unpack_from('<I',image,0x3c)[0]
        count=struct.unpack_from('<H',image,pe+6)[0];size=struct.unpack_from('<H',image,pe+20)[0];sections=[]
        for index in range(count):
            offset=pe+24+size+40*index
            virtual,address,raw=struct.unpack_from('<III',image,offset+8)
            flags=struct.unpack_from('<I',image,offset+36)[0]
            sections.append((address,address+max(virtual,raw),flags))
        definitions=[]
        for block in re.findall(r'Export \{(.*?)\}',output,re.S):
            name=re.search(r'Name: (\S+)',block)[1]
            if only is not None and name not in only:continue
            address=int(re.search(r'RVA: (0x[0-9A-Fa-f]+)',block)[1],16)
            code=any(start<=address<end and flags&0x20000000 for start,end,flags in sections)
            definitions.append(name+('' if code else ' DATA'))
        assert definitions and (only is None or {name.removesuffix(' DATA') for name in definitions}==set(only))
        definition=imports/(dll.stem+'.def');library=definition.with_suffix('.a')
        definition.write_text('LIBRARY '+dll.name+'\nEXPORTS\n'+'\n'.join(definitions)+'\n',encoding='ascii')
        run([llvm/'llvm-dlltool.exe','-m','i386:x86-64','-d',definition,'-l',library])
        return library
    dependencies={};links=[]
    for relative_name in ('bin/libsvn_subr-1.dll','bin/libaprutil-1.dll','bin/libapr-1.dll','apache/bin/libhttpd.dll'):
        path=ROOT/'runtime/pgvn'/relative_name
        expected_dependency=(manifest['apache_dependencies'] if relative_name.startswith('apache/') else manifest['runtime_dependencies'])[relative_name]
        assert sha(path)==expected_dependency;dependencies[path.relative_to(ROOT).as_posix()]=expected_dependency
        links.append(import_library(path))
    flags=[option for option in manifest['flags'] if option not in ('-DNDEBUG','-Werror')]
    flags+=['-I',str(svn/'subversion/mod_dav_svn'),'-Wno-deprecated-declarations']
    products={};providers={}
    for backend in ('fsfs','pgvn'):
        provider=(Path(inputs['apache'])/'modules/mod_dav_svn.so' if backend=='fsfs' else
                  ROOT/'runtime/pgvn/apache/modules/mod_dav_pgvn.so')
        provider_expected=('f7c580dfc0c41cc9a3655c1e03688731dfdbabd00cf55971aede51b5472a9121' if backend=='fsfs' else
                           manifest['products'][provider.relative_to(ROOT).as_posix()]['sha256'])
        assert sha(provider)==provider_expected;providers[backend]={'path':str(provider),'sha256':provider_expected}
        function='dav_svn_split_uri' if backend=='fsfs' else 'dav_pgvn_split_uri'
        library=import_library(provider,[function]);target=OUT/backend/'mod_dontdothat.so';target.parent.mkdir(exist_ok=True)
        redirect=[] if backend=='fsfs' else ['-include',str(namespace)]
        run([compiler,*flags,*redirect,source,'-shared','-Wl,--export-all-symbols','-Wl,--no-insert-timestamp',
             library,*links,'-o',target])
        exported=run([llvm/'llvm-readobj.exe','--coff-exports',target]);assert 'Name: dontdothat_module' in exported
        imported=run([llvm/'llvm-readobj.exe','--coff-imports',target])
        assert 'Name: '+provider.name+'\n' in imported and function in imported
        wrong='mod_dav_pgvn.so' if backend=='fsfs' else 'mod_dav_svn.so';assert wrong not in imported
        products[backend]={'path':target.relative_to(ROOT).as_posix(),'sha256':sha(target),
                           'bytes':target.stat().st_size,'imports':imported,'exports':exported}
    assert sha(source)==expected and sha(namespace)==manifest['dav_patches']['namespace_sha256']
    assert sha(manifest_path)==frozen
    report={'status':'BUILT_NOT_PROTOCOL_ACCEPTED','build_manifest_sha256':frozen,'builder_sha256':sha(ROOT/SELF),
            'archive_sha256':sha(archive),'source':relative,'source_sha256':expected,
            'namespace_sha256':sha(namespace),'compiler_sha256':sha(compiler),'products':products,
            'providers':providers,'dependencies':dependencies,'commands':commands,
            'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/build/upstream-http.json';path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'products':{key:value['sha256'] for key,value in products.items()}}))
    return 0


if __name__=='__main__':raise SystemExit(main())
