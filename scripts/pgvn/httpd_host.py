"""Build the pinned Apache entry with the unchanged signed SDK core DLLs."""
from pathlib import Path
import hashlib
import json
import re


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_sources(root,source,sdk):
    lock_path=root/'src/pgvn/httpd/source-lock.json'
    lock=json.loads(lock_path.read_text(encoding='utf-8'))
    if lock['version']!='2.4.68':raise ValueError('Unexpected Apache host version')
    for name,record in lock['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('Invalid source path')
        path=source/name
        if not path.is_file() or digest(path)!=record['sha256']:
            raise ValueError('Apache host source is missing or changed: '+str(path)+'; run scripts/pgvn/fetch_httpd.py')
    for path in (source/'include/ap_release.h',sdk/'include/ap_release.h'):
        text=path.read_text(encoding='utf-8')
        for key,value in (('MAJORVERSION_NUMBER','2'),('MINORVERSION_NUMBER','4'),('PATCHLEVEL_NUMBER','68')):
            if not re.search(r'#define\s+AP_SERVER_'+key+r'\s+'+value+r'\b',text):
                raise ValueError('Apache host source/SDK version mismatch')
    return {'version':lock['version'],'source_lock_sha256':digest(lock_path),
        'sources':{name:record['sha256'] for name,record in lock['files'].items()}}


def compile_resource(root,build,tools,run):
    source=root/'src/pgvn/windows/utf8.manifest'
    rc=build/'generated/pgvn-utf8.rc'
    rc.write_text('1 24 "'+source.as_posix()+'"\n',encoding='utf-8')
    result=build/'objects/pgvn-utf8.o'
    run([tools/'llvm-windres.exe','--target=pe-x86-64','-i',rc,'-O','coff','-o',result])
    return result


def compile_host(root,source,build,tools,options,libraries,resource,output,run):
    main=build/'objects/httpd-main.o'
    run([tools/'clang.exe',*[o for o in options if o!='-Werror'],'-DAPP_FILE','-Dmain=pgvn_httpd_main',
        '-c',source/'server/main.c','-o',main])
    run([tools/'clang.exe',*options,'-municode',root/'src/pgvn/httpd/entry.c',main,resource,
        libraries['libhttpd.dll'],libraries['libapr-1.dll'],libraries['libaprutil-1.dll'],
        '-Wl,--no-insert-timestamp','-o',output])
