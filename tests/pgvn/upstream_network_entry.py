"""Run unchanged upstream assertions with explicit native or stock fixtures.

Only test setup/lifecycle/config/tool helpers are replaced for native mode.
No test function, decorator, test_list or expected result is rewritten.
"""
from pathlib import Path
import configparser
import hashlib
import importlib
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[2]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    configuration=Path(sys.argv[1]).resolve();config=json.loads(configuration.read_text(encoding='utf-8'))
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
    assert sha(manifest_path)==config['manifest_sha256'];manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    for name in ('upstream_network_entry.py','upstream_hook_bridge.py'):
        assert sha(ROOT/'tests/pgvn'/name)==manifest['sources']['tests/pgvn/'+name]
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
    results=[];events=work/'fixture-events.jsonl';repositories={};policy={};hook_paths={};purged=set()
    registry_lock=threading.RLock()
    def event(action,**values):
        with events.open('a',encoding='utf-8') as output:output.write(json.dumps({'event':action,**values})+'\n')
    def full(path):return Path(path).resolve()
    def admin(*args,expected=0):
        env=dict(os.environ,PGVN_DSN=f"host=127.0.0.1 port=16432 dbname={config['database']} user=pgvn_admin sslmode=disable")
        result=subprocess.run([str(ROOT/'runtime/pgvn/bin/pgvnadmin.exe'),*map(str,args),'--json'],
            cwd=work,env=env,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
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
    original_open=main.open_pipe
    def open_pipe(command,*args,**kwargs):
        if native:
            refresh_registry()
            # Some upstream cases use the *local* repository format file only
            # as arbitrary upload data. Supply the same bytes used by the
            # completed FSFS control, outside every native repository alias.
            if str(command[0])==main.svnmucc_binary:
                command=list(command)
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
                'bridge_sha256':sha(bridge),'repositories':{v:str(k) for k,v in repositories.items()}})
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
        admin('config','publish','id:'+identifier,'--expected-generation',current['generation'],
              '--authz',authz,'--hooks',hooks,'--settings',settings)
        event('publish',repository_id=identifier)
    def post_create(path,minor_version=None):
        path=full(path);refresh_registry();assert path in repositories and not path.exists()
        policy[path]={'authz':'[/]\n* = rw\n','hooks':{},'settings':{
            'identity_domain':config['identity_domain'],'anonymous_access':'read','authenticated_access':'write'}}
        publish(path)
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
        absolute=full(path)
        if absolute not in hook_paths:
            assert absolute.parent.name=='hooks'
            path=hook_path(absolute.parent.parent,absolute.name);absolute=full(path)
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
    original_set_name=svntest.sandbox.Sandbox._set_name
    def set_name(sbox,*args,**kwargs):
        original_set_name(sbox,*args,**kwargs);folder=policy_paths(sbox.repo_dir)
        sbox.authz_file=str(folder/'authz');sbox.groups_file=str(folder/'groups')
    if native:
        os.environ.update(PGVN_UPSTREAM_DATABASE=config['database'],PGVN_UPSTREAM_ROOT=work.as_posix(),
            PGVN_UPSTREAM_LEDGER=(work/'native-commands.jsonl').as_posix(),
            PGVN_DSN=f"host=127.0.0.1 port=16432 dbname={config['database']} user=pgvn_admin sslmode=disable")
        main.create_repos=create;main._post_create_repos=post_create
        main.safe_rmtree=remove;main.chmod_tree=chmod;main.create_python_hook_script=hook
        main.write_authz_file=authz;svntest.sandbox.Sandbox._set_name=set_name
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
    module=importlib.import_module(config['suite']);before=list(module.test_list)
    assert all(0<i<len(before) for i in config['cases'])
    status='FAIL';error=None;exit_code=1
    try:
        exit_code=main.execute_tests(module.test_list,serial_only=True,test_selection=selection)
        if native:refresh_registry()
        assert module.test_list==before,'Original test_list changed'
        assert sorted(r['id'] for r in results)==sorted(config['cases']),'Missing or repeated upstream outcomes'
        assert all(not path.exists() for path in repositories),'Native alias materialized'
        verify_snapshot();status='PASS' if exit_code==0 else 'FAIL'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr,flush=True)
    imported={name:Path(value.__file__).resolve().relative_to(snapshot).as_posix()
              for name,value in sys.modules.items() if getattr(value,'__file__',None)
              and Path(value.__file__).resolve().is_relative_to(snapshot)}
    result={'status':status,'exit_code':exit_code,'error':error,'cases':results,'backend':config['backend'],
            'suite':config['suite'],'selected':config['cases'],'imported_upstream_modules':imported,
            'original_test_list_preserved':module.test_list==before,
            'native_aliases':{str(k):v for k,v in repositories.items()},
            'fixture_boundaries':['tool paths','create/configure repository','logical repository cleanup/chmod',
                                  'hook Python body argv/stdin translation','Sandbox policy file locations','authz publication',
                                  'local format-file upload sample redirected to byte-identical stock-control data'] if native else ['helper executable paths'],
            'pending_boundaries':['Native hook bridge runs the supplied Python body; Windows cmd_alternative is not executed.',
                                  'Direct physical repository file operations and historical disk formats need case applicability review.'] if native else []}
    if config.get('tls_authority'):
        result['fixture_boundaries']+=['private CA in default client configuration; explicit caller TLS settings retained',
                                      'direct HTTPS helper verifies certificate chain and hostname']
        result['tls_authority_sha256']=sha(Path(config['tls_authority']))
        result['certificate_exception_cache_files']=[str(p.relative_to(work)) for p in work.rglob('*')
            if p.is_file() and 'svn.ssl.server' in p.parts]
    (work/'entry-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
