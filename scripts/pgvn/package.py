"""Create a checksummed Windows x64 runtime without developer-path fallbacks.

The deployment target needs Windows PowerShell and an independent PostgreSQL
server. Python and the SDKs are build-time inputs only. --development produces
an explicitly unaccepted snapshot while M6/M7 acceptance is still in progress.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pe_dependencies import closure
from windows_manifest import embed_utf8

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def data_json(value):return (json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False)+'\n').encode('utf-8')
def stable_write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=data:raise RuntimeError('Existing output differs; preserve it and choose another output root: '+str(path))
    else:
        with path.open('xb') as stream:stream.write(data)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--templates',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--development',action='store_true')
    args=parser.parse_args();root=args.root.resolve();runtime=root/'runtime/pgvn'
    templates=(args.templates or root/'deploy/windows').resolve()
    output=(args.output or runtime/'packages').resolve()
    manifest_path=root/'artifacts/pgvn/build/manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['configuration']!='Release':raise RuntimeError('Only the frozen Release can be packaged')
    sys.path.insert(0,str(root/'tests/pgvn'))
    from test_m4 import build_identity
    if build_identity()!=manifest:raise RuntimeError('Build identity changed')
    for relative,meta in manifest['products'].items():
        if digest(root/relative)!=meta['sha256']:raise RuntimeError('Product digest mismatch: '+relative)
    acceptance=None
    if not args.development:
        path=root/'artifacts/pgvn/tests/m6-latest.json'
        acceptance=json.loads(path.read_text(encoding='utf-8'))
        if acceptance.get('status')!='PASS' or acceptance.get('milestone_complete') is not True:
            raise RuntimeError('Complete M6 acceptance is required for a release package')
        if acceptance.get('build_manifest_sha256')!=digest(manifest_path):
            raise RuntimeError('M6 acceptance does not match this frozen build')

    required=('PgVN.Common.ps1','Initialize-PgVNConfiguration.ps1','Install-PgVNServices.ps1',
        'Uninstall-PgVNServices.ps1','Test-PgVNPackage.ps1','Invoke-PgVNAdmin.ps1',
        'Set-PgVNUser.ps1','Backup-PgVN.ps1','Restore-PgVN.ps1','Test-PgVNDeployment.ps1',
        'README.zh-CN.md','pgvnserve.ini.example','pgvn-admin.ini.example',
        'pgvn-migrate.ini.example','pgvn-restore.ini.example',
        'pgvn-apache.ini.example','pg_service.conf.example','pgpass.conf.example',
        'pg_service.runtime.conf.example','runtime.pgpass.conf.example',
        'httpd.conf.example','openssl.cnf')
    for name in required:
        if not (templates/name).is_file():raise RuntimeError('Missing deployment template: '+name)

    groups={}
    def group(name,entries,search,folder,module_folder=None):
        files,audit=closure(entries,search)
        groups[name]={'files':files,'audit':audit,'folder':folder,'module_folder':module_folder}
    native=runtime/'bin';apache=runtime/'apache';pg=Path(manifest['inputs']['postgres'])
    group('pgvn',[native/n for n in ('pgvnserve.exe','pgvnadmin.exe')],[native],'bin')
    modules=[runtime/n for n in manifest['apache_dependencies'] if n.startswith('apache/modules/')]
    modules += [apache/'modules/mod_dav_pgvn.so',apache/'modules/mod_authz_pgvn.so']
    # APR crypto can be selected by the runtime rather than through a PE import.
    apache_entries=[apache/'bin'/n for n in ('pgvnhttpd.exe','htpasswd.exe','openssl.exe')]+modules
    if (apache/'bin/apr_crypto_openssl-1.dll').is_file():apache_entries.append(apache/'bin/apr_crypto_openssl-1.dll')
    group('apache',apache_entries,[apache/'bin'],'apache/bin','apache/modules')
    clients=[native/(name+'.exe') for name in ('svn','svnmucc','svnrdump','svnsync','svnversion')]
    clients += [native/n for n in ('libsvn_fs_fs-1.dll','libsvn_fs_x-1.dll') if (native/n).is_file()]
    group('svn-clients',clients,[native],'tools/svn')
    group('postgresql-clients',[pg/'bin'/(n+'.exe') for n in ('psql','pg_dump','pg_restore','pg_isready')],
        [pg/'bin',native],'tools/postgresql')

    # PostgreSQL clients are unsigned. Derive private copies with the same
    # UTF-8 process policy; signed Apache/SVN SDK images stay byte-for-byte.
    policy=root/'src/pgvn/windows/utf8.manifest'
    system=Path(os.environ['SystemRoot'])/'System32'
    windows_policy={'manifest_sha256':digest(policy),
        'transformer_sha256':digest(Path(__file__).with_name('windows_manifest.py')),
        'resource_writer':{n:digest(system/n) for n in ('kernel32.dll','KernelBase.dll')}}
    transform_base=root/'build/pgvn/package-utf8'
    transform_base.mkdir(parents=True,exist_ok=True)
    transform_directory=Path(tempfile.mkdtemp(prefix='run-',dir=transform_base))
    prepared={};transforms={}
    for basename,source in groups['postgresql-clients']['files'].items():
        if source.suffix.lower()!='.exe':continue
        target=transform_directory/source.name
        shutil.copyfile(source,target)
        record=embed_utf8(target,policy)
        prepared['postgresql-clients/'+basename]=target
        transforms['tools/postgresql/'+source.name]=record

    svn_source=Path(manifest['inputs']['svn_bin']).parent
    apache_source=Path(manifest['inputs']['apache'])
    apr=Path(manifest['inputs']['apr_include']).parent
    apu=Path(manifest['inputs']['apu_include']).parent
    licenses={'Subversion-LICENSE.txt':svn_source/'LICENSE.txt','Subversion-NOTICE.txt':svn_source/'NOTICE.txt',
        'Apache-LICENSE.txt':apache_source/'LICENSE.txt','Apache-NOTICE.txt':apache_source/'NOTICE.txt',
        'APR-LICENSE.txt':apr/'LICENSE','APR-NOTICE.txt':apr/'NOTICE',
        'APR-util-LICENSE.txt':apu/'LICENSE','APR-util-NOTICE.txt':apu/'NOTICE',
        'PostgreSQL-server-license.txt':pg/'server_license.txt',
        'PostgreSQL-client-third-party-licenses.txt':pg/'commandlinetools_3rd_party_licenses.txt'}
    host_source=Path(manifest['inputs']['apache_source'])
    licenses['Apache-host-source-LICENSE.txt']=host_source/'LICENSE'
    licenses['Apache-host-source-NOTICE.txt']=host_source/'NOTICE'
    for path in licenses.values():
        if not path.is_file():raise RuntimeError('Missing vendor license input: '+str(path))
    documentation=sorted((root/'docs/pgvn').glob('*.md'))
    support=[]
    for pattern in ('docs/plans/*.md','config/*example*','sql/*.sql','sql/pgvn/*.sql',
        'docs/pgvn/evidence/*.json','docs/pgvn/images/*.jpg','web/pgvn-monitor/*'):
        support += [p for p in sorted(root.glob(pattern)) if p.is_file() and not p.name.startswith('m7-package')]
    input_identity={'build_manifest':digest(manifest_path),'packager':digest(Path(__file__)),
        'pe_reader':digest(Path(__file__).with_name('pe_dependencies.py')),
        'templates':{n:digest(templates/n) for n in required},
        'licenses':{n:digest(p) for n,p in licenses.items()},
        'documentation':{p.name:digest(p) for p in documentation},
        'support_files':{p.relative_to(root).as_posix():digest(p) for p in support},
        'native':{name:{n:digest(prepared.get(name+'/'+n,p)) for n,p in record['files'].items()} for name,record in groups.items()},
        'windows_process_policy':windows_policy,'executable_manifest_transforms':transforms,
        'development':args.development}
    package_id=hashlib.sha256(data_json(input_identity)).hexdigest()[:16]
    stem='PgVN-'+manifest['milestone']+'-windows-x64-'+package_id
    destination=output/stem
    files={};provenance={};native_audit={}
    def put(relative,data,source=None):
        if Path(relative).is_absolute() or '..' in Path(relative).parts:raise ValueError('Invalid package path')
        stable_write(destination/relative,data)
        files[relative]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
        if source is not None:provenance[relative]={'source':str(source),'source_sha256':digest(source)}
    def copy(relative,source,bom=False):
        data=Path(source).read_bytes()
        if bom:data=data.removeprefix(b'\xef\xbb\xbf');data=b'\xef\xbb\xbf'+data
        put(relative,data,source)

    for name,record in groups.items():
        targets={}
        for basename,source in record['files'].items():
            folder=record['module_folder'] if source.suffix.lower()=='.so' and record['module_folder'] else record['folder']
            relative=folder+'/'+source.name
            selected=prepared.get(name+'/'+basename)
            if selected:put(relative,selected.read_bytes(),source)
            else:copy(relative,source)
            targets[basename]=relative
        native_audit[name]={'files':targets,**record['audit']}
    for name in required:
        if name.endswith('.ps1'):relative='deploy/windows/'+name
        elif name=='README.zh-CN.md':relative=name
        else:relative='config.templates/'+name
        if name=='README.zh-CN.md':
            text=(templates/name).read_text(encoding='utf-8').replace('(../../docs/pgvn/','(docs/pgvn/')
            put(relative,text.encode('utf-8'),templates/name)
        else:copy(relative,templates/name,bom=name.endswith('.ps1'))
    for name,path in licenses.items():copy('licenses/'+name,path)
    html=(root/'web/pgvn-monitor/index.html').read_text(encoding='utf-8')
    start=html.index('MIT License');end=html.index('-->',start)
    put('licenses/Phosphor-Icons-MIT.txt',html[start:end].strip().encode('utf-8')+b'\n')
    for path in documentation:
        relative='docs/pgvn/'+path.name
        text=path.read_text(encoding='utf-8').replace('(../../deploy/windows/README.zh-CN.md)','(../../README.zh-CN.md)')
        text=re.sub(r'\[[^\]]+\]\(evidence/m7-package[^()]*\.json\)',
            '`'+stem+'.validation.json`（随 ZIP 单独提供）',text)
        put(relative,text.encode('utf-8'),path)
    for path in sorted((root/'docs/plans').glob('*.md')):copy('docs/plans/'+path.name,path)
    for path in sorted((root/'config').glob('*example*')):
        if path.is_file():copy('config/'+path.name,path)
    for path in sorted((root/'sql').glob('*.sql')):copy('schema-reference/'+path.name,path)
    for path in sorted((root/'sql/pgvn').glob('*.sql')):copy('schema-reference/pgvn/'+path.name,path)
    copy('build-manifest.json',manifest_path)
    copy('source-reference/httpd-host-lock.json',root/'src/pgvn/httpd/source-lock.json')
    # Archive validation describes this archive's hash and is delivered as a
    # sidecar / source-repository record, avoiding self-referential inputs.
    for path in sorted((root/'docs/pgvn/evidence').glob('*.json')):
        if not path.name.startswith('m7-package'):copy('docs/pgvn/evidence/'+path.name,path)
    for path in sorted((root/'docs/pgvn/images').glob('*.jpg')):copy('docs/pgvn/images/'+path.name,path)
    # This is a reference copy; the page is embedded and needs no static server.
    for path in sorted((root/'web/pgvn-monitor').iterdir()):
        if path.is_file():copy('embedded-web-reference/'+path.name,path)
    put('THIRD_PARTY_NOTICES.zh-CN.txt',(
        '依赖来自本次固定 SDK 分发，逐文件来源和摘要见 package-manifest.json。\n'
        'Subversion、Apache、APR/APR-util、PostgreSQL 及其运行依赖的随附许可证保存在 licenses。\n'
        'Phosphor 图标 MIT 声明同时内嵌于监控 HTML。VC 运行库为固定供应商分发中的应用本地依赖。\n'
        '没有将 Windows 系统 DLL、测试探针、旧 svnserve、配置密钥或数据库密码打入包中。\n'
        'pgvnhttpd 为固定 Apache 入口源码构建，使用未改写的 SDK libhttpd/APR。\n'
        '四个未签名 PostgreSQL 客户端的私有副本加入 UTF-8 进程清单；来源/前后摘要及资源校验见包清单。\n'
    ).encode('utf-8'))
    package={'format':1,'package_id':package_id,'product':'PgVN','milestone':manifest['milestone'],
        'configuration':'Release','architecture':'x64','created_at':manifest['created_at'],
        'validation_status':'development-snapshot' if args.development else 'M6-accepted-runtime',
        'clean_machine_acceptance':False,'polarion_application_acceptance':False,
        'requirements':['Windows x64 supporting UTF-8 process manifests (Windows 10 1903+) with HTTP.sys and Windows PowerShell','Independent PostgreSQL server; PostgreSQL 18.6 tested'],
        'build_manifest_sha256':digest(manifest_path),'git_base_commit':manifest['git_base_commit'],
        'git_worktree_dirty_at_build':manifest['git_worktree_dirty'],
        'inputs':input_identity,'files':files,'provenance':provenance,'native_dependency_audit':native_audit,
        'windows_process_policy':windows_policy,'executable_manifest_transforms':transforms,
        'acceptance':acceptance}
    encoded=data_json(package);stable_write(destination/'package-manifest.json',encoded)
    checksum=hashlib.sha256(encoded).hexdigest()
    stable_write(destination/'package-manifest.json.sha256',(checksum+'  package-manifest.json\n').encode())
    expected=set(files)|{'package-manifest.json','package-manifest.json.sha256'}
    actual={p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    if actual!=expected:raise RuntimeError('Unexpected files in the immutable package staging directory')
    for relative,meta in files.items():
        if digest(destination/relative)!=meta['sha256']:raise RuntimeError('Packaged file changed: '+relative)
    if build_identity()!=manifest:raise RuntimeError('Frozen build changed while packaging')
    archive=output/(stem+'.zip')
    scratch=output/(stem+'.zip.part-'+str(os.getpid()))
    with zipfile.ZipFile(scratch,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
        for relative in sorted(expected):
            info=zipfile.ZipInfo(stem+'/'+relative,date_time=(2020,1,1,0,0,0))
            info.create_system=0;info.compress_type=zipfile.ZIP_DEFLATED
            bundle.writestr(info,(destination/relative).read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    if archive.exists():
        if digest(archive)!=digest(scratch):raise RuntimeError('Existing archive differs; preserve it for inspection')
        scratch.unlink()
    else:scratch.replace(archive)
    archive_hash=digest(archive)
    stable_write(archive.with_suffix('.zip.sha256'),(archive_hash+'  '+archive.name+'\n').encode())
    print(json.dumps({'package_directory':str(destination),'archive':str(archive),'sha256':archive_hash,
        'files':len(files),'bytes':archive.stat().st_size,'validation_status':package['validation_status']}))
    return 0
if __name__=='__main__':raise SystemExit(main())
