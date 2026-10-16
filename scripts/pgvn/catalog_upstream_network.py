"""Verify pinned upstream inputs and catalog network-fixture adapter boundaries.

This never imports upstream modules, executes tests, starts servers or accesses
databases. A successful catalog is not a compatibility result or exclusion list.
"""
from pathlib import Path, PurePosixPath
from collections import Counter, defaultdict
import argparse
import ast
import datetime as dt
import hashlib
import io
import json
import re
import sys
import tokenize
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[2]
RESULTS=ROOT/'artifacts/pgvn/tests'
LOCK=ROOT/'tests/pgvn/upstream_network_lock.py'
SCRIPT='scripts/pgvn/catalog_upstream_network.py'
LOCK_RELATIVE='tests/pgvn/upstream_network_lock.py'
CMDLINE='subversion/tests/cmdline'

class InputRejected(ValueError):pass

def require(condition,message):
    if not condition:raise InputRejected(message)

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def parse_python(path):
    data=path.read_bytes();encoding,_=tokenize.detect_encoding(io.BytesIO(data).readline)
    return ast.parse(data.decode(encoding),filename=str(path))

def read_lock():
    require(LOCK.stat().st_size<2*1024*1024,'Source lock exceeds the size limit')
    values={}
    for node in parse_python(LOCK).body:
        if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):continue
        require(isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name),'Source lock must contain literal assignments only')
        name=node.targets[0].id;require(name not in values,'Duplicate source-lock field')
        values[name]=ast.literal_eval(node.value)
    expected={'FORMAT_VERSION','SUBVERSION_VERSION','ARCHIVE_BASENAME','ARCHIVE_SHA256','ARCHIVE_ROOT','PROVENANCE','FILES'}
    require(set(values)==expected and values['FORMAT_VERSION']==1,'Unsupported source-lock schema')
    require(values['SUBVERSION_VERSION']=='1.14.5','Unsupported upstream version')
    require(re.fullmatch('[0-9a-f]{64}',values['ARCHIVE_SHA256']) is not None,'Invalid archive digest')
    require(Path(values['ARCHIVE_BASENAME']).name==values['ARCHIVE_BASENAME'],'Archive name must be a basename')
    require(values['ARCHIVE_ROOT']=='subversion-1.14.5','Unexpected archive root')
    require(isinstance(values['FILES'],dict) and values['FILES'],'Empty source lock')
    total=0
    for name,metadata in values['FILES'].items():
        relative=PurePosixPath(name)
        require(not relative.is_absolute() and '..' not in relative.parts and '\\' not in name and str(relative)==name,'Unsafe source-lock path')
        require(set(metadata)=={'sha256','size'} and re.fullmatch('[0-9a-f]{64}',metadata['sha256']) is not None,'Invalid file metadata')
        require(type(metadata['size']) is int and 0<=metadata['size']<=32*1024*1024,'Invalid input size')
        total+=metadata['size']
    require(total<=128*1024*1024,'Locked inputs exceed the total size limit')
    return values

def generated(path):
    return '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo')

def verify_inputs(source,archive,lock):
    require(digest(archive)==lock['ARCHIVE_SHA256'],'Archive SHA-256 differs from the committed source lock')
    with zipfile.ZipFile(archive) as z:
        members={info.filename:info for info in z.infolist()}
        require(len(members)==len(z.infolist()),'Archive contains duplicate member names')
        for name,metadata in lock['FILES'].items():
            member=members.get(lock['ARCHIVE_ROOT']+'/'+name)
            require(member is not None and not member.is_dir() and member.file_size==metadata['size'],'Missing or resized archive member: '+name)
            require(hashlib.sha256(z.read(member)).hexdigest()==metadata['sha256'],'Archive member differs from source lock: '+name)
            path=source/name
            require(path.is_file() and path.resolve().is_relative_to(source),'Missing or escaping vendor file: '+name)
            require(path.stat().st_size==metadata['size'] and digest(path)==metadata['sha256'],'Vendor file differs from source lock: '+name)
    expected={name for name in lock['FILES'] if name.startswith(CMDLINE+'/')}
    actual={p.relative_to(source).as_posix() for p in (source/CMDLINE).rglob('*') if p.is_file() and not generated(p)}
    require(actual==expected,'Vendor cmdline file set differs from lock: '+repr({'extra':sorted(actual-expected)[:8],'missing':sorted(expected-actual)[:8]}))
    header=(source/'subversion/include/svn_version.h').read_text(encoding='utf-8')
    version='.'.join(re.search(r'^#define\s+SVN_VER_'+part+r'\s+(\d+)',header,re.M)[1] for part in ('MAJOR','MINOR','PATCH'))
    require(version==lock['SUBVERSION_VERSION'],'Header version differs from source lock')

def materialize_snapshot(archive,lock,run_id):
    parent=(ROOT/'runtime/pgvn/upstream-source').resolve()
    require(parent.is_relative_to(ROOT.resolve()),'Snapshot parent escapes the repository')
    snapshot=parent/run_id;snapshot.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        for name,metadata in lock['FILES'].items():
            member=z.getinfo(lock['ARCHIVE_ROOT']+'/'+name);data=z.read(member)
            require(hashlib.sha256(data).hexdigest()==metadata['sha256'],'Snapshot source mismatch: '+name)
            target=snapshot/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            mode=(member.external_attr>>16)&0o777
            if mode:target.chmod(mode)
    marker=snapshot/'.pgvn-source-snapshot.json'
    marker.write_text(json.dumps({'source_lock_sha256':digest(LOCK),'archive_sha256':lock['ARCHIVE_SHA256'],'files':lock['FILES']},indent=2)+'\n',encoding='utf-8')
    actual={p.relative_to(snapshot).as_posix() for p in snapshot.rglob('*') if p.is_file()}
    require(actual==set(lock['FILES'])|{marker.name},'Fresh snapshot has unexpected files')
    for name,metadata in lock['FILES'].items():require(digest(snapshot/name)==metadata['sha256'],'Snapshot checksum mismatch: '+name)
    return {'root':str(snapshot),'cmdline':str(snapshot/CMDLINE),'files':len(lock['FILES']),
        'manifest_sha256':digest(marker),'upstream_modules_imported':False,'bytecode_files':0}

def dotted(node):
    if isinstance(node,ast.Name):return node.id
    if isinstance(node,ast.Attribute):return dotted(node.value)+'.'+node.attr
    return '<dynamic>'

HELPERS={'create_repos','copy_repos','_post_create_repos','unpack_greek_repos','run_command','run_command_stdin',
    'run_svnadmin','run_svnlook','run_svnrdump','write_authz_file','write_restrictive_svnserve_conf','create_python_hook',
    'build','simple_repo_copy','add_repo_path','clone_dependent','cleanup_test_paths'}
TOOL_OFFSETS={'run_svnadmin':('svnadmin',0),'run_svnlook':('svnlook',0),'run_svnrdump':('svnrdump',1),
    'run_and_verify_svnadmin':('svnadmin',2),'run_and_verify_svnlook':('svnlook',2),
    'run_svn':('svn',1),'run_and_verify_svn':('svn',2),'run_and_verify_svn2':('svn',3)}
FILE_OPERATIONS={'open','file_write','file_append','safe_rmtree','safe_rm','makedirs','mkdir','remove','unlink','copyfile','copytree','rename'}

def operations(tree):
    calls=Counter();tools=defaultdict(Counter);examples=[];path_hints=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):continue
        name=dotted(node.func);leaf=name.rsplit('.',1)[-1]
        if leaf in HELPERS or leaf.startswith('run_') and 'svn' in leaf:calls[name]+=1
        tool=None;offset=None
        if leaf in TOOL_OFFSETS:tool,offset=TOOL_OFFSETS[leaf]
        elif leaf in ('run_command','run_command_stdin') and node.args:
            binary=dotted(node.args[0]).rsplit('.',1)[-1]
            if binary.endswith('_binary'):
                tool=binary[:-7];offset=3 if leaf=='run_command' else 5
        if tool is not None:
            operation=node.args[offset] if len(node.args)>offset else None
            operation=operation.value if isinstance(operation,ast.Constant) and isinstance(operation.value,str) else '<dynamic>'
            tools[tool][operation]+=1
            if len(examples)<30:examples.append({'line':node.lineno,'call':name,'tool':tool,'operation':operation})
        if leaf in FILE_OPERATIONS and any(isinstance(n,ast.Attribute) and n.attr=='repo_dir' for arg in node.args for n in ast.walk(arg)):
            path_hints.append({'line':node.lineno,'call':name})
    return {'helper_calls':dict(sorted(calls.items())),'tool_operation_syntax':{k:dict(sorted(v.items())) for k,v in sorted(tools.items())},
        'tool_call_examples':examples,'direct_repo_path_io_syntax_hints':path_hints,
        'hint_scope':'Syntactic call sites only; indirect aliases and runtime paths are not inferred. These are neither exclusions nor compatibility verdicts.'}

def suite_catalog(source,path):
    tree=parse_python(path)
    assignments=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='test_list' for t in n.targets)]
    require(len(assignments)==1 and isinstance(assignments[0].value,ast.List),'Unsupported test_list construction: '+path.name)
    entries=assignments[0].value.elts
    require(entries and isinstance(entries[0],ast.Constant) and entries[0].value is None,'Missing upstream test_list sentinel: '+path.name)
    for node in ast.walk(tree):
        require(not (isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name) and node.target.id=='test_list'),'Dynamic test_list mutation: '+path.name)
        require(not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='test_list'),'Dynamic test_list method: '+path.name)
    definitions={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    cases=[]
    for index,entry in enumerate(entries[1:],1):
        require(isinstance(entry,ast.Name) and entry.id in definitions,'Unresolved upstream test callable: '+path.name+':'+str(index))
        function=definitions[entry.id]
        cases.append({'id':index,'name':entry.id,'line':function.lineno,'decorators':[ast.unparse(d) for d in function.decorator_list]})
    return {'file':path.relative_to(source).as_posix(),'sha256':digest(path),'test_count':len(cases),'tests':cases,**operations(tree)}

def catalog(source):
    suites={path.stem[:-6]:suite_catalog(source,path) for path in sorted((source/CMDLINE).glob('*_tests.py'))}
    require(suites,'No upstream suites discovered')
    anchors=[]
    for relative in ('svntest/main.py','svntest/sandbox.py'):
        path=source/CMDLINE/relative;tree=parse_python(path)
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in HELPERS:
                anchors.append({'file':(PurePosixPath(CMDLINE)/relative).as_posix(),'name':node.name,'line':node.lineno,
                    'arguments':[a.arg for a in node.args.args],'calls':operations(node)})
    return suites,anchors

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-manifest',type=Path,default=ROOT/'artifacts/pgvn/build/manifest.json')
    parser.add_argument('--source-root',type=Path);parser.add_argument('--archive',type=Path)
    parser.add_argument('--require-frozen-inputs',action='store_true')
    parser.add_argument('--snapshot',action='store_true',help='Create a fresh verified source tree without vendor bytecode caches')
    parser.add_argument('--output',type=Path,default=RESULTS/'m6-network-catalog-latest.json')
    args=parser.parse_args();output=args.output.resolve()
    if not output.is_relative_to(RESULTS.resolve()):parser.error('--output must stay under artifacts/pgvn/tests')
    run_id='m6-network-catalog-'+uuid.uuid4().hex[:12]
    report={'test_id':run_id,'status':'INPUT_REJECTED','milestone_complete':False,'tests_executed':0,'generated_exclusions':[],
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'scope':'Source identity and static catalog only. No test assertions executed, no protocol/FSFS/HA/performance compatibility conclusion.',
        'script_sha256':digest(Path(__file__)),'python_version':sys.version,
        'frozen_input_scope':[SCRIPT,LOCK_RELATIVE],
        'future_execution_requirement':'Run upstream code from a fresh verified source snapshot without consuming vendor bytecode caches. This catalog never imports upstream modules.'}
    try:
        report['source_lock_sha256']=digest(LOCK)
        manifest=json.loads(args.build_manifest.read_text(encoding='utf-8'));lock=read_lock()
        source=(args.source_root or Path(manifest['inputs']['subversion'])).resolve()
        archive=(args.archive or source.parent/lock['ARCHIVE_BASENAME']).resolve()
        frozen=manifest['sources'].get(SCRIPT)==digest(Path(__file__)) and manifest['sources'].get(LOCK_RELATIVE)==digest(LOCK)
        require(not args.require_frozen_inputs or frozen,'Catalog script/source lock not registered in this frozen build')
        report.update(build_manifest_sha256=digest(args.build_manifest),source_root=str(source),archive=str(archive),
            archive_sha256=digest(archive),version=lock['SUBVERSION_VERSION'],provenance=lock['PROVENANCE'],frozen_inputs=frozen)
        verify_inputs(source,archive,lock)
        suites,anchors=catalog(source)
        if args.snapshot:report['snapshot']=materialize_snapshot(archive,lock,run_id)
        # Recheck immutable inputs after parsing; do not turn a mid-run edit into a catalog.
        verify_inputs(source,archive,lock)
        report.update(status='CATALOG_ONLY',source_identity_verified=True,locked_files=len(lock['FILES']),suite_count=len(suites),
            test_count=sum(s['test_count'] for s in suites.values()),suites=suites,fixture_anchors=anchors)
        exit_code=0
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error);exit_code=1
    RESULTS.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2,ensure_ascii=False)+'\n'
    (RESULTS/(run_id+'.json')).write_text(data,encoding='utf-8');output.write_text(data,encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('test_id','status','suite_count','test_count','tests_executed','frozen_inputs','error') if k in report}))
    return exit_code

if __name__=='__main__':raise SystemExit(main())
