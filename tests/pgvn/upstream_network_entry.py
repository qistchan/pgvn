"""Run unchanged upstream assertions with explicit native or stock fixtures.

Only test setup/lifecycle/config/tool helpers are replaced for native mode.
No test function, decorator, test_list or expected result is rewritten.
"""
from pathlib import Path
import configparser
import hashlib
import importlib
import importlib.util
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlparse,quote
from urllib.request import url2pathname

ROOT=Path(__file__).resolve().parents[2]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    configuration=Path(sys.argv[1]).resolve();config=json.loads(configuration.read_text(encoding='utf-8'))
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    assert sha(manifest_path)==config['manifest_sha256'];manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    for name in ('upstream_network_entry.py','upstream_hook_bridge.py'):
        assert sha(ROOT/'tests/pgvn'/name)==manifest['sources']['tests/pgvn/'+name]
    policy_source=ROOT/'tests/pgvn/upstream_network_policy.py'
    assert sha(policy_source)==manifest['sources']['tests/pgvn/upstream_network_policy.py']
    policy_spec=importlib.util.spec_from_file_location('pgvn_upstream_policy',policy_source)
    policy_boundary=importlib.util.module_from_spec(policy_spec);policy_spec.loader.exec_module(policy_boundary)
    output_source=ROOT/'tests/pgvn/upstream_hook_output.py'
    assert sha(output_source)==manifest['sources']['tests/pgvn/upstream_hook_output.py']
    output_spec=importlib.util.spec_from_file_location('pgvn_upstream_hook_output',output_source)
    output_boundary=importlib.util.module_from_spec(output_spec);output_spec.loader.exec_module(output_boundary)
    copy_source=ROOT/'tests/pgvn/upstream_network_copy.py'
    assert sha(copy_source)==manifest['sources']['tests/pgvn/upstream_network_copy.py']
    copy_spec=importlib.util.spec_from_file_location('pgvn_upstream_copy',copy_source)
    copy_boundary=importlib.util.module_from_spec(copy_spec);copy_spec.loader.exec_module(copy_boundary)
    log_source=ROOT/'tests/pgvn/upstream_network_log.py'
    assert sha(log_source)==manifest['sources']['tests/pgvn/upstream_network_log.py']
    log_spec=importlib.util.spec_from_file_location('pgvn_upstream_log',log_source)
    log_boundary=importlib.util.module_from_spec(log_spec);log_spec.loader.exec_module(log_boundary)
    work=Path(config['work']).resolve();os.chdir(work)
    assert work.is_relative_to((ROOT/'runtime/pgvn/tests').resolve()) and configuration.is_relative_to(work)
    native=config['backend']=='pgvn';assert config['backend'] in ('pgvn','fsfs')
    if native:assert re.fullmatch(r'pgvn_m6_net_[0-9a-f]{12}',config['database'])
    snapshot=Path(config['snapshot']).resolve();marker=json.loads((snapshot/'.pgvn-source-snapshot.json').read_text(encoding='utf-8'))
    assert sha(snapshot/'.pgvn-source-snapshot.json')==config['snapshot_manifest_sha256']
    def verify_snapshot():
        for name,record in marker['files'].items():assert sha(snapshot/name)==record['sha256'],name
        assert not list(snapshot.rglob('*.pyc')) and not list(snapshot.rglob('__pycache__'))
    verify_snapshot();cmdline=snapshot/'subversion/tests/cmdline'
    sys.path.insert(0,str(cmdline));import svntest
    main=svntest.main;actions=svntest.actions
    results=[];events=work/'fixture-events.jsonl';repositories={};policy={};hook_paths={};purged=set();declared=set()
    registry_lock=threading.RLock()
    def event(action,**values):
        with events.open('a',encoding='utf-8') as output:output.write(json.dumps({'event':action,**values})+'\n')
    def full(path):return Path(path).resolve()
    def admin_process(*args):
        env=dict(os.environ,PGVN_DSN=f"host=127.0.0.1 port=16432 dbname={config['database']} user=pgvn_admin sslmode=disable")
        return subprocess.run([str(ROOT/'runtime/pgvn/bin/pgvnadmin.exe'),*map(str,args),'--json'],
            cwd=work,env=env,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    def admin(*args,expected=0):
        result=admin_process(*args)
        if result.returncode!=expected:raise svntest.Failure('Native fixture admin failed: '+result.stderr.decode('utf-8','replace'))
        return json.loads(result.stdout) if result.stdout else None
    def bind_tools():
        for name in ('svn','svnadmin','svnlook','svnrdump','svnsync','svndumpfilter','svnversion','svnmucc'):
            setattr(main,name+'_binary',str(ROOT/'runtime/pgvn/bin'/(name+'.exe')))
        helpers=ROOT/'runtime/pgvn/upstream-helpers/bin'
        for name,binary in (('entriesdump','entries-dump'),('wc_lock_tester','wc-lock-tester'),
                            ('wc_incomplete_tester','wc-incomplete-tester'),('atomic_ra_revprop_change','atomic-ra-revprop-change'),
                            ('lock_helper','lock-helper' if native else 'lock-helper-fsfs')):
            setattr(main,name+'_binary',str(helpers/(binary+'.exe')))
        if native:
            for name in ('svnadmin','svnlook'):
                setattr(main,name+'_binary',str(ROOT/'runtime/pgvn/upstream-tools/bin'/('pgvn-upstream-'+name+'.exe')))
    bind_tools()
    original_config=main.create_config_dir
    def create_config(*args,**kwargs):
        bind_tools();result=original_config(*args,**kwargs)
        supplied_servers=kwargs.get('server_contents',args[2] if len(args)>2 else None)
        if isinstance(supplied_servers,bytes):supplied_servers=supplied_servers.decode('utf-8','replace')
        explicit_tls=supplied_servers is not None and re.search(r'^\s*ssl-[a-z-]+\s*=',supplied_servers,re.M|re.I)
        if config.get('tls_authority') and not explicit_tls:
            authority=Path(config['tls_authority']).resolve()
            assert authority.is_relative_to(work.parent/'tls') and authority.is_file()
            # Upstream --ssl-cert caches an accepted certificate exception.
            # Give the normal SVN TLS verifier a private CA instead. Repeated
            # [global] sections preserve caller settings using SVN's INI rules.
            directory=args[0] if args else kwargs['cfgdir']
            with (Path(directory)/'servers').open('a',encoding='utf-8') as stream:
                stream.write('\n[global]\nssl-authority-files = '+authority.as_posix()+'\nssl-trust-default-ca = no\n')
        return result
    main.create_config_dir=create_config
    original_http=main.create_http_connection
    def create_http_connection(url,debuglevel=9):
        parsed=urlparse(url)
        if parsed.scheme=='https' and config.get('tls_authority'):
            assert parsed.hostname in ('localhost','127.0.0.1'),'TLS helper target is outside the loopback fixture'
            connection=http.client.HTTPSConnection(parsed.hostname,parsed.port,
                context=ssl.create_default_context(cafile=config['tls_authority']))
            connection.set_debuglevel(debuglevel);return connection
        return original_http(url,debuglevel)
    main.create_http_connection=create_http_connection
    log_fixture=None
    original_open=main.open_pipe
    def open_pipe(command,*args,**kwargs):
        if native:
            refresh_registry()
            if log_fixture is not None:command=log_fixture.command(command,main.svn_binary)
            # Some upstream cases use the *local* repository format file only
            # as arbitrary upload data. Supply the same bytes used by the
            # completed FSFS control, outside every native repository alias.
            if str(command[0])==main.svnmucc_binary:
                command=list(command)
                for index in range(1,len(command)-1):
                    if command[index]!='-U' or urlparse(str(command[index+1])).scheme!='file':continue
                    local=urlparse(str(command[index+1]));assert local.netloc in ('','localhost')
                    alias=full(url2pathname(local.path));assert alias in repositories and not alias.exists()
                    assert policy[alias]['authz']=='[/]\n* = rw\n' and not policy[alias]['hooks']
                    assert policy[alias]['settings']['authenticated_access']=='write'
                    target=config.get('local_fixture_url');assert target and re.fullmatch(r'svn://127\.0\.0\.1:[0-9]+',target)
                    original_url=command[index+1]
                    command[index+1]=target+'/'+quote(alias.relative_to(work).as_posix(),safe='/')
                    event('local-fixture-write',repository_id=repositories[alias],original_url=original_url,
                          url=command[index+1],method='Actual native SVN write replaces legacy file:// setup; the update under test retains its original URL.')
                for index in range(1,len(command)-2):
                    if command[index]!='put':continue
                    source=full(command[index+1])
                    if source.name!='format' or source.parent not in repositories:continue
                    control=Path(config['fsfs_control_work']).resolve()
                    assert control.parent==work.parent and control.name=='fsfs'
                    original=control/source.relative_to(work)
                    assert original.is_file() and original.resolve().is_relative_to(control)
                    data=original.read_bytes();target=work/'global-fixture-inputs'/repositories[source.parent]/'format'
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                    assert target.read_bytes()==data and not source.exists()
                    command[index+1]=str(target)
                    event('fixture-input',operation='svnmucc put',alias=str(source),
                          control_source=str(original),source=str(target),bytes=len(data),sha256=sha(original))
        event('tool',program=str(command[0]))
        return original_open(command,*args,**kwargs)
    main.open_pipe=open_pipe
    original_print=main.TestRunner._print_name
    def print_name(runner,prefix):
        original_print(runner,prefix)
        record={'id':runner.index,'name':runner.get_function_name(),'result':prefix.strip().rstrip(':')}
        results.append(record);event('upstream-result',**record)
    main.TestRunner._print_name=print_name
    bridge=ROOT/'tests/pgvn/upstream_hook_bridge.py';bridge_config=work/'hook-context.json'
    def refresh_registry():
        with registry_lock:
            ledger=work/'native-commands.jsonl'
            if ledger.exists():
                deadline=time.monotonic()+3
                while True:
                    try:
                        content=ledger.read_text(encoding='utf-8')
                        if content and not content.endswith('\n'):raise ValueError('Incomplete native ledger append')
                        records=[json.loads(line) for line in content.splitlines()];break
                    except (PermissionError,UnicodeDecodeError,ValueError):
                        if time.monotonic()>=deadline:raise
                        time.sleep(.01)
                for record in records:
                    alias=bytes.fromhex(record['alias_hex']).decode('utf-8')
                    if not alias.startswith('pgvn:') and record['repository_id'] not in purged:
                        repositories[full(alias)]=record['repository_id']
            content=json.dumps({'work':str(work),'database':config['database'],
                'bridge_sha256':sha(bridge),'hook_output_sha256':sha(output_source),
                'manifest_sha256':config['manifest_sha256'],'repositories':{v:str(k) for k,v in repositories.items()}})
            if bridge_config.exists() and bridge_config.read_text(encoding='utf-8')==content:return
            temporary=bridge_config.with_suffix('.tmp');temporary.write_text(content,encoding='utf-8')
            deadline=time.monotonic()+3
            while True:
                try:temporary.replace(bridge_config);break
                except PermissionError:
                    # A Windows reader can briefly deny replacement. Keep the
                    # previous complete context visible and retry this publish.
                    if time.monotonic()>=deadline:raise
                    time.sleep(.01)
    def policy_paths(path):
        key=hashlib.sha256(str(full(path)).encode('utf-8')).hexdigest()[:20]
        parent=work/'global-policy'/key;parent.mkdir(parents=True,exist_ok=True)
        return parent
    def publish(path):
        path=full(path);refresh_registry();identifier=repositories[path];folder=policy_paths(path)
        state=policy[path];authz=folder/'authz';hooks=folder/'hooks.json';settings=folder/'settings.json'
        authz.write_text(state['authz'],encoding='utf-8');hooks.write_text(json.dumps(state['hooks']),encoding='utf-8')
        settings.write_text(json.dumps(state['settings']),encoding='utf-8')
        current=admin('repo','show','id:'+identifier)
        translated,mapping=policy_boundary.namespace(state['authz'],path,current['name'])
        native_authz=folder/'authz.native';native_authz.write_text(translated,encoding='utf-8')
        group_file=folder/'groups';groups=None
        if state.get('separate_groups') and group_file.exists():groups=group_file.read_text(encoding='utf-8')
        arguments=['config','publish','id:'+identifier,'--expected-generation',current['generation'],
                   '--authz',native_authz,'--hooks',hooks,'--settings',settings]
        if groups is not None:arguments+=['--groups',group_file]
        result=admin_process(*arguments)
        if result.returncode:
            if not policy_boundary.authz_rejection(result):
                raise svntest.Failure('Native policy publication failed: '+result.stderr.decode('utf-8','replace'))
            injected=policy_boundary.invalid_file(config,manifest,work,current,translated,groups,state['hooks'],state['settings'])
            event('invalid-authz-file',**injected)
        event('authz-namespace',repository_id=identifier,**mapping)
        event('publish',repository_id=identifier)
    def post_create(path,minor_version=None):
        path=full(path);refresh_registry();assert path in repositories and not path.exists()
        policy[path]={'authz':'[/]\n* = rw\n','hooks':{},'settings':{
            'identity_domain':config['identity_domain'],'anonymous_access':'read','authenticated_access':'write'}}
        publish(path)
        if config['url'].startswith(('http:','https:')) and path.parent==work/'svn-test-work/repositories':
            current=admin('repo','show','id:'+repositories[path])
            event('ddt-route',**policy_boundary.route_alias(config,manifest,work,current,path))
    def create(path,minor_version=None):
        if minor_version is not None and minor_version!=14:
            raise svntest.Failure('Native fixture has no historical per-repository disk format: '+str(minor_version))
        assert full(path).is_relative_to(work) and full(path)!=work
        code,out,err=main.run_command(main.svnadmin_binary,1,False,'create',path,'--fs-type=pg','--compatible-version=1.14')
        if code or err:raise main.SVNRepositoryCreateFailure(''.join(err))
        post_create(path,minor_version)
    original_remove=main.safe_rmtree;original_chmod=main.chmod_tree
    def remove(path,*args,**kwargs):
        absolute=full(path);assert absolute.is_relative_to(work) and absolute!=work,'Cleanup escapes fixture root'
        refresh_registry()
        if absolute in repositories:
            identifier=repositories[absolute];admin('repo','disable','id:'+identifier)
            admin('maintenance','purge','id:'+identifier,'--confirm-id',identifier)
            purged.add(identifier);del repositories[absolute];policy.pop(absolute,None)
            event('purge',repository_id=identifier,alias=str(absolute));return
        return original_remove(path,*args,**kwargs)
    def chmod(path,*args,**kwargs):
        refresh_registry()
        if full(path) in repositories:
            assert not full(path).exists();event('directory-free-mode',alias=str(full(path)));return
        return original_chmod(path,*args,**kwargs)
    def hook_path(repo,kind):
        refresh_registry();identifier=repositories[full(repo)]
        folder=work/'global-hooks'/identifier;folder.mkdir(parents=True,exist_ok=True)
        path=folder/kind;hook_paths[full(path)]=(full(repo),kind)
        return str(path)
    original_hook=main.create_python_hook_script
    def hook(path,code,cmd_alternative=None):
        absolute=full(path);refresh_registry()
        if absolute not in hook_paths:
            repository=full(absolute.parent.parent) if absolute.parent.name=='hooks' else None
            if repository not in repositories:
                assert absolute.is_relative_to(work) and absolute!=work
                assert not any(absolute.is_relative_to(alias) for alias in set(repositories)|declared)
                candidates=(absolute,Path(str(absolute)+'.py'),Path(str(absolute)+'.bat'))
                assert all(candidate.resolve().is_relative_to(work) and not any(candidate.resolve().is_relative_to(alias) for alias in set(repositories)|declared) for candidate in candidates)
                result=original_hook(path,code,cmd_alternative)
                files={str(candidate):sha(candidate) for candidate in candidates if candidate.is_file()}
                assert files,'Original generic script helper produced no files'
                event('generic-script-helper',path=str(absolute),files=files,cmd_alternative=cmd_alternative is not None,
                      supplied_python_sha256=hashlib.sha256(code.encode('utf-8') if isinstance(code,str) else bytes(code)).hexdigest())
                return result
            path=hook_path(repository,absolute.name);absolute=full(path)
        repo,kind=hook_paths[absolute]
        original_hook(str(absolute),code,cmd_alternative)
        # The Python body is the portable original helper input. The optional
        # Windows cmd_alternative is not executed by this ID fixture.
        script=absolute.with_name(absolute.name+'.py');script.write_text(code,encoding='utf-8')
        policy[repo]['hooks'][kind]={'executable':Path(sys.executable).name,
            'arguments':['-I','-B',str(bridge),str(bridge_config),str(script)],'timeout_seconds':30}
        publish(repo);event('hook',kind=kind,script_sha256=sha(script),cmd_alternative=cmd_alternative is not None)
    original_authz=main.write_authz_file
    def authz(sbox,*args,**kwargs):
        original_authz(sbox,*args,**kwargs);repo=full(sbox.repo_dir)
        policy[repo]['authz']=Path(sbox.authz_file).read_text(encoding='utf-8');publish(repo)
    original_conf_path=main.get_svnserve_conf_file_path
    def conf_path(repo):
        refresh_registry();path=full(repo)
        return str(policy_paths(path)/'svnserve.conf') if path in repositories else original_conf_path(repo)
    original_restrictive=main.write_restrictive_svnserve_conf
    def restrictive(repo,anon_access='none',separate_groups_db=False):
        result=original_restrictive(repo,anon_access,separate_groups_db)
        path=full(repo)
        if config['url'].startswith('svn:'):
            policy[path]['settings']['anonymous_access']=anon_access
            policy[path]['settings']['authenticated_access']='write'
        policy[path]['separate_groups']=separate_groups_db
        publish(path);event('svnserve-config',repository_id=repositories[path],anonymous_access=anon_access,
                            separate_groups=separate_groups_db,path=conf_path(repo))
        return result
    original_groups=main.write_groups_file
    def groups(sbox,*args,**kwargs):
        result=original_groups(sbox,*args,**kwargs);publish(sbox.repo_dir);return result
    original_set_name=svntest.sandbox.Sandbox._set_name
    def set_name(sbox,*args,**kwargs):
        original_set_name(sbox,*args,**kwargs);declared.add(full(sbox.repo_dir));folder=policy_paths(sbox.repo_dir)
        sbox.authz_file=str(folder/'authz');sbox.groups_file=str(folder/'groups')
    original_add_repo=svntest.sandbox.Sandbox.add_repo_path;original_copytree=shutil.copytree
    def add_repo(sbox,*args,**kwargs):
        path,url=original_add_repo(sbox,*args,**kwargs);declared.add(full(path));return path,url
    def copytree(source,destination,*args,**kwargs):
        refresh_registry();first=full(source);second=full(destination)
        if first not in repositories and second not in declared:
            return original_copytree(source,destination,*args,**kwargs)
        value=copy_boundary.request(config,work,first,second,repositories,declared,policy,args,kwargs)
        copied=copy_boundary.clone(value,admin)
        repositories[second]=copied['destination_id'];post_create(second)
        admin('repo','enable','id:'+copied['destination_id'])
        assert not first.exists() and not second.exists()
        event('native-copytree',**copied)
        return destination
    if native:
        if config['suite']=='log_tests':
            log_fixture=log_boundary.LogFixtures(config,work,repositories,declared,event)
            original_file_write=main.file_write;original_copy_repos=main.copy_repos
            def file_write(path,contents,*args,**kwargs):
                refresh_registry()
                return log_fixture.write(original_file_write,path,contents,*args,**kwargs)
            def copy_repos(source,destination,head_revision,ignore_uuid=1,minor_version=None):
                if minor_version in (None,14):
                    return original_copy_repos(source,destination,head_revision,ignore_uuid,minor_version)
                refresh_registry()
                value=log_fixture.history_request(source,destination,head_revision,ignore_uuid,minor_version)
                return log_fixture.copy_history(value,create,admin,refresh_registry)
            main.file_write=file_write;main.copy_repos=copy_repos
        output_boundary.install(work,lambda:repositories)
        os.environ.update(PGVN_UPSTREAM_DATABASE=config['database'],PGVN_UPSTREAM_ROOT=work.as_posix(),
            PGVN_UPSTREAM_LEDGER=(work/'native-commands.jsonl').as_posix(),
            PGVN_DSN=f"host=127.0.0.1 port=16432 dbname={config['database']} user=pgvn_admin sslmode=disable")
        main.create_repos=create;main._post_create_repos=post_create
        main.safe_rmtree=remove;main.chmod_tree=chmod;main.create_python_hook_script=hook
        main.get_svnserve_conf_file_path=conf_path;main.write_restrictive_svnserve_conf=restrictive
        main.write_groups_file=groups
        main.write_authz_file=authz;svntest.sandbox.Sandbox._set_name=set_name
        svntest.sandbox.Sandbox.add_repo_path=add_repo;shutil.copytree=copytree
        for kind in ('start-commit','pre-commit','post-commit','pre-revprop-change','pre-lock','pre-unlock'):
            setattr(main,'get_'+kind.replace('-','_')+'_hook_path',lambda repo,k=kind:hook_path(repo,k))
    sys.argv=[str(cmdline/(config['suite']+'.py')),'--url='+config['url'],
              '--bin='+str(ROOT/'runtime/pgvn/bin'),'--fs-type='+('pg' if native else 'fsfs'),
              '--server-minor-version=14','--keep-local-tmp','--srcdir='+str(snapshot),
              *map(str,config['cases'])]
    if config.get('httpd_version'):sys.argv.append('--httpd-version='+config['httpd_version'])
    # Upstream's default arglist is captured when its module is imported.
    _,selection=main.parse_options(sys.argv[1:])
    assert not main.options.development and not main.options.list_tests and not main.options.parallel
    import_source=ROOT/'tests/pgvn/upstream_network_import.py'
    assert sha(import_source)==manifest['sources']['tests/pgvn/upstream_network_import.py']
    import_spec=importlib.util.spec_from_file_location('pgvn_upstream_import',import_source)
    import_boundary=importlib.util.module_from_spec(import_spec);import_spec.loader.exec_module(import_boundary)
    upgrades=importlib.import_module('upgrade_tests')
    if os.name=='nt':import_boundary.install_working_copy_moves(upgrades,config,snapshot,marker,event)
    if native:
        def legacy_repository(sbox,tar_filename,dir=None):
            return import_boundary.legacy_repository(sbox,tar_filename,dir,config,snapshot,marker,create,remove,event)
        upgrades.replace_sbox_repo_with_tarfile=legacy_repository
    module=importlib.import_module(config['suite']);before=list(module.test_list)
    assert all(0<i<len(before) for i in config['cases'])
    status='FAIL';error=None;exit_code=1
    try:
        exit_code=main.execute_tests(module.test_list,serial_only=True,test_selection=selection)
        if native:refresh_registry()
        assert module.test_list==before,'Original test_list changed'
        assert sorted(r['id'] for r in results)==sorted(config['cases']),'Missing or repeated upstream outcomes'
        assert all(not path.exists() for path in set(repositories)|declared),'Native alias materialized'
        verify_snapshot();status='PASS' if exit_code==0 else 'FAIL'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr,flush=True)
    imported={name:Path(value.__file__).resolve().relative_to(snapshot).as_posix()
              for name,value in sys.modules.items() if getattr(value,'__file__',None)
              and Path(value.__file__).resolve().is_relative_to(snapshot)}
    result={'status':status,'exit_code':exit_code,'error':error,'cases':results,'backend':config['backend'],
            'suite':config['suite'],'selected':config['cases'],'imported_upstream_modules':imported,
            'original_test_list_preserved':module.test_list==before,
            'native_aliases':{str(k):v for k,v in repositories.items()},
            'declared_native_aliases':sorted(map(str,declared)),
            'native_aliases_absent':all(not path.exists() for path in set(repositories)|declared),
            'fixture_boundaries':['tool paths','create/configure repository','logical repository cleanup/chmod',
                                  'hook Python body argv/stdin translation','Sandbox policy file locations','authz publication',
                                  'ordinary client script helpers retain the original Python/batch creation within non-repository fixture paths',
                                  'local format-file upload sample redirected to byte-identical stock-control data',
                                  'svnserve config/groups files relocated to global policy and versioned settings',
                                  'exact authz repository namespace mapped to canonical ID-fixture name',
                                  'admin-rejected malformed authz appended as non-superuser fixture file state',
                                  'historical FSFS archive imported with stock dump and native load, preserving UUID/revisions',
                                  'DDT Apache parent alias represented by a guarded second catalog route',
                                  'only hooks.log scratch I/O relocated to global hook output with an operation audit',
                                  'log-suite registered log-msg input and its matching SVN -F/--file argument relocated together; bytes unchanged',
                                  'log-suite historical copy input round-trips through real SVN 1.5-compatible stock FSFS and native dump/load',
                                  'registered repository copytree setup uses a verified production hotcopy; relocation/copy assertions unchanged',
                                  'legacy file:// svnmucc setup uses a same-database loopback SVN writer; tested updates retain their URLs'] if native else ['helper executable paths'],
            'pending_boundaries':['Native hook bridge runs the supplied Python body; Windows cmd_alternative is not executed.',
                                  'Direct physical repository file operations and historical disk formats need case applicability review.'] if native else []}
    if os.name=='nt':
        result['fixture_boundaries']+=['locked historical WC archive setup uses a bounded same-volume rename retry on both backends; bytes/attributes preserved']
    if config.get('tls_authority'):
        result['fixture_boundaries']+=['private CA in default client configuration; explicit caller TLS settings retained',
                                      'direct HTTPS helper verifies certificate chain and hostname']
        result['tls_authority_sha256']=sha(Path(config['tls_authority']))
        result['certificate_exception_cache_files']=[str(p.relative_to(work)) for p in work.rglob('*')
            if p.is_file() and 'svn.ssl.server' in p.parts]
    (work/'entry-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
