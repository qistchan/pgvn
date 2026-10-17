"""Runs only inside the disposable, network-isolated Ubuntu QEMU guest."""
from pathlib import Path
import hashlib,json,os,pwd,shutil,subprocess,sys,time,urllib.error,urllib.request

WORK=Path('/var/lib/pgvn-vm');WORK.mkdir(mode=0o700,exist_ok=True)
PHASE=WORK/'phase.json';SUMMARY=WORK/'summary.json';PACKAGE=Path('/opt/fixture/package')
CTL=PACKAGE/'deploy/linux/pgvnctl.py';CURRENT=Path('/opt/pgvn/current')
env=dict(os.environ,PGSERVICEFILE='/etc/pgvn/pg_service.conf',PGPASSFILE='/etc/pgvn/pgpass',PGVN_DSN='service=pgvn-owner',LANG='C.UTF-8')
checks=[];commands=[];password='vm-fixture-páss'


def run(args,data=None,expected=0,timeout=90,as_user=None,environment=None):
    arguments=list(map(str,args));arguments=['runuser','-u',as_user,'--',*arguments] if as_user else arguments
    result=subprocess.run(arguments,input=data,text=True,capture_output=True,env=environment or env,timeout=timeout)
    commands.append({'args':arguments,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode!=expected:raise RuntimeError((arguments,result.returncode,result.stdout,result.stderr))
    return result.stdout


def check(name,condition=True):
    if not condition:raise AssertionError(name)
    checks.append(name);print(json.dumps({'check':name,'status':'PASS'}),flush=True)


def until(test,seconds=120):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if test():return
        except (OSError,RuntimeError):pass
        time.sleep(.25)
    raise RuntimeError('Guest readiness deadline exceeded')


def health(path):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18033'+path,timeout=2) as response:return response.status
    except urllib.error.HTTPError as response:return response.code


def admin(*args,data=None):
    return run([CURRENT/'bin/pgvnadmin',*args],data=data,as_user='pgvn')


def svn(*args):
    return run(['/usr/bin/svn',*args,'--non-interactive','--no-auth-cache','--config-dir',WORK/'svn-config','--username','writer','--password-from-stdin'],data=password+'\n')


def sql(statement,database='postgres'):
    return run(['/opt/postgresql/bin/psql','-XAt','-h','127.0.0.1','-U','postgres','-d',database,'-v','ON_ERROR_STOP=1','-c',statement])


def systemctl(*args):return run(['systemctl',*args])


def first_boot():
    check('fresh Ubuntu root has no installed PgVN',not CURRENT.exists() and not Path('/etc/pgvn').exists())
    check('systemd detects the real QEMU VM',run(['systemd-detect-virt','--vm']).strip()=='qemu')
    container=subprocess.run(['systemd-detect-virt','--container'],text=True,capture_output=True)
    check('systemd uses full VM lifecycle',container.returncode!=0 and container.stdout.strip()=='none')
    os.umask(0o077)
    initial=Path('/opt/fixture/previous-package')
    if not initial.exists():initial=PACKAGE
    run(['python3',CTL,'install','--package',initial]);run(['python3',CTL,'init'])
    check('actual guest install and key initialization')
    run(['systemd-analyze','verify','/etc/systemd/system/pgvnserve.service','/etc/systemd/system/pgvnhttpd.service']);check('systemd validates both deployed units')
    account=pwd.getpwnam('pgvn');postgres=pwd.getpwnam('postgres')
    etc=Path('/etc/pgvn')
    services=''
    for alias,user in [('pgvn-owner','pgvn_owner'),('pgvn-service','pgvn_service'),('pgvn-monitor','pgvn_monitor')]:
        services+='['+alias+']\nhost=127.0.0.1\ndbname=pgvn\nuser='+user+'\nsslmode=disable\nconnect_timeout=3\n'
    (etc/'pg_service.conf').write_text(services);os.chown(etc/'pg_service.conf',0,account.pw_gid);(etc/'pg_service.conf').chmod(0o640)
    (etc/'pgpass').write_text('');os.chown(etc/'pgpass',account.pw_uid,account.pw_gid);(etc/'pgpass').chmod(0o600)
    data=Path('/var/lib/pgsql');data.mkdir(mode=0o700);os.chown(data,postgres.pw_uid,postgres.pw_gid)
    run(['/opt/postgresql/bin/initdb','-D',data/'data','--encoding=UTF8','--locale=C.UTF-8','--auth-local=trust','--auth-host=trust'],as_user='postgres')
    unit='[Unit]\nDescription=Isolated fixture PostgreSQL 18.6\nAfter=network.target\n[Service]\nType=simple\nUser=postgres\nGroup=postgres\nExecStart=/opt/postgresql/bin/postgres -D /var/lib/pgsql/data -c listen_addresses=127.0.0.1 -c shared_buffers=32MB -c max_connections=64\nTimeoutStopSec=30\n[Install]\nWantedBy=multi-user.target\n'
    Path('/etc/systemd/system/pgvn-test-postgres.service').write_text(unit)
    Path('/etc/systemd/system/pgvn-test-postgres.service').chmod(0o644)
    systemctl('daemon-reload');systemctl('enable','--now','pgvn-test-postgres.service')
    until(lambda:'1' in sql('SELECT 1'))
    check('actual guest PostgreSQL server is 18.6',sql('SHOW server_version_num').strip()=='180006')
    sql('CREATE ROLE pgvn_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; CREATE ROLE pgvn_service LOGIN NOSUPERUSER; CREATE ROLE pgvn_monitor LOGIN NOSUPERUSER;')
    sql('CREATE DATABASE pgvn OWNER pgvn_owner')
    admin('db','init');admin('db','grant','--service-role','pgvn_service','--monitor-role','pgvn_monitor')
    admin('user','set','pgvn','writer','--key-file',etc/'keys/current.key',data=password+'\n')
    repository=json.loads(admin('repo','create','vm-repository','--route','/svn/vm'))
    rules=WORK/'authz';rules.write_text('[/]\nwriter = rw\n* =\n');rules.chmod(0o644)
    # The owner CLI runs under the same protected-key UID as the service.
    WORK.chmod(0o755)
    admin('config','publish',repository['id'],'--expected-generation',repository['generation'],'--authz',rules)
    check('PostgreSQL 18.6 schema and restricted roles initialized')
    systemctl('stop','pgvn-test-postgres.service')
    systemctl('enable','pgvnserve.service','pgvnhttpd.service')
    systemctl('start','pgvnserve.service')
    until(lambda:health('/health/live')==200)
    check('systemd service lives while PG starts late',health('/health/ready')==503)
    systemctl('start','pgvn-test-postgres.service');until(lambda:health('/health/ready')==200)
    check('systemd service becomes ready after PG recovery')
    config=(PACKAGE/'deploy/linux/httpd.conf.example').read_text();(etc/'httpd.conf').write_text(config)
    os.chown(etc/'httpd.conf',0,account.pw_gid);(etc/'httpd.conf').chmod(0o640)
    run([CURRENT/'apache/bin/htpasswd','-i','-c','-m',etc/'http-users','writer'],data=password+'\n')
    os.chown(etc/'http-users',0,account.pw_gid);(etc/'http-users').chmod(0o640)
    systemctl('start','pgvnhttpd.service')
    check('real systemd Apache unit loads matching modules',systemctl('is-active','pgvnhttpd.service').strip()=='active')
    url='svn://127.0.0.1:3690/svn/vm';svn('mkdir',url+'/trunk','-m','VM initial commit')
    wc=WORK/'wc';svn('checkout',url,wc);content='真实 Linux 虚拟机 · 重启后保留\n'
    (wc/'trunk/readme.txt').write_text(content);svn('add',wc/'trunk/readme.txt');svn('commit',wc,'-m','VM content commit')
    check('actual guest native and DAV read same content',svn('cat','http://127.0.0.1:8080/svn/vm/trunk/readme.txt')==content)
    before=systemctl('show','-p','MainPID','--value','pgvnserve.service').strip()
    systemctl('kill','--kill-whom=main','--signal=KILL','pgvnserve.service')
    until(lambda:systemctl('show','-p','MainPID','--value','pgvnserve.service').strip() not in ('0',before) and health('/health/ready')==200)
    check('systemd restarts crashed server',int(systemctl('show','-p','NRestarts','--value','pgvnserve.service'))>=1)
    if initial!=PACKAGE:
        previous_prefix=CURRENT.resolve();key_bytes=(etc/'keys/current.key').read_bytes()
        owner_config=WORK/'owner.ini';owner_config.write_text('[pgvn]\nconnection = service=pgvn-owner\n');owner_config.chmod(0o600)
        head=svn('info','--show-item','revision',url).strip()
        for action,expected_prefix in [('upgrade',None),('rollback',previous_prefix),('upgrade',None)]:
            systemctl('stop','pgvnserve.service','pgvnhttpd.service')
            arguments=['python3',CTL,action,'--nodes-stopped']
            arguments+=['--config',owner_config] if action=='rollback' else ['--package',PACKAGE]
            run(arguments)
            current=CURRENT.resolve()
            check(action+' switches to verified compatible package',current==expected_prefix if expected_prefix else current!=previous_prefix)
            systemctl('start','pgvnserve.service','pgvnhttpd.service');until(lambda:health('/health/ready')==200)
            check(action+' preserves key, HEAD and both protocols',
                  (etc/'keys/current.key').read_bytes()==key_bytes and svn('info','--show-item','revision',url).strip()==head
                  and svn('cat',url+'/trunk/readme.txt')==content and svn('cat','http://127.0.0.1:8080/svn/vm/trunk/readme.txt')==content)
        check('active package matches final Release manifest',
              (CURRENT/'package-manifest.json').read_bytes()==(PACKAGE/'package-manifest.json').read_bytes())
    scratch=Path('/var/lib/pgvn/vm-hooks');scratch.mkdir(mode=0o700);os.chown(scratch,account.pw_uid,account.pw_gid)
    shutil.copy2('/opt/fixture/hook-child',scratch/'probe-child');os.chown(scratch/'probe-child',account.pw_uid,account.pw_gid);(scratch/'probe-child').chmod(0o700)
    probe_env={**env,'LD_LIBRARY_PATH':str(CURRENT/'lib')}
    hook=json.loads(run(['/opt/fixture/hook-exec-probe',scratch,CURRENT.resolve()/'bin/pgvn-hook-runner'],as_user='pgvn',environment=probe_env))
    check('actual guest kernel reaps escaped Hook descendants',hook['status']=='PASS' and hook['assertions']==58)
    phase={'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'repository_id':repository['id'],'content':content,
           'head':svn('info','--show-item','revision',url).strip(),'checks':checks,'commands':commands,'hook':hook}
    PHASE.write_text(json.dumps(phase,ensure_ascii=False));PHASE.chmod(0o600)
    os.sync();print('PGVN_VM_REBOOT_REQUEST',flush=True);subprocess.run(['systemctl','--no-block','reboot'],check=True,timeout=10)


def second_boot():
    phase=json.loads(PHASE.read_text());checks.extend(phase['checks']);commands.extend(phase['commands'])
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    check('real guest kernel rebooted',boot_id!=phase['boot_id'])
    for unit in ('pgvnserve.service','pgvnhttpd.service','pgvn-test-postgres.service'):
        check(unit+' enabled across reboot',systemctl('is-enabled',unit).strip()=='enabled')
        until(lambda:systemctl('is-active',unit).strip()=='active')
    until(lambda:health('/health/ready')==200)
    check('native service ready after unattended reboot')
    url='svn://127.0.0.1:3690/svn/vm'
    check('repository HEAD survives reboot',svn('info','--show-item','revision',url).strip()==phase['head'])
    check('credential key and native content survive reboot',svn('cat',url+'/trunk/readme.txt')==phase['content'])
    check('DAV content survives reboot',svn('cat','http://127.0.0.1:8080/svn/vm/trunk/readme.txt')==phase['content'])
    systemctl('reload','pgvnhttpd.service')
    check('Apache graceful reload preserves service',svn('cat','http://127.0.0.1:8080/svn/vm/trunk/readme.txt')==phase['content'])
    start=time.monotonic();systemctl('stop','pgvnserve.service','pgvnhttpd.service');elapsed=time.monotonic()-start
    check('systemd stop respects deadline',elapsed<30)
    for unit in ('pgvnserve.service','pgvnhttpd.service'):
        check(unit+' exits cleanly',systemctl('show','-p','Result','--value',unit).strip()=='success' and systemctl('show','-p','ExecMainStatus','--value',unit).strip()=='0')
    check('stopped units release all business database sessions',sql("SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'pgvn%' AND datname='pgvn'",'pgvn').strip()=='0')
    admin('verify',phase['repository_id']);check('history verifies after reboot and drain')
    summary={'format':1,'status':'PASS','checks':checks,'commands':commands,'first_boot_id':phase['boot_id'],'second_boot_id':boot_id,
             'kernel':run(['uname','-a']).strip(),'os_release':Path('/etc/os-release').read_text(),'stop_seconds':elapsed,
             'package_manifest_sha256':hashlib.sha256((PACKAGE/'package-manifest.json').read_bytes()).hexdigest(),
             'build_identity':json.loads((PACKAGE/'build-manifest.json').read_text())['build_identity'],
             'system_packages':run(['dpkg-query','-W','-f=${Package}=${Version}\n']).splitlines(),'virtualization':'QEMU TCG; functional tests, no performance claim'}
    SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');SUMMARY.chmod(0o600)
    os.sync();print('PGVN_VM_STATUS PASS',flush=True);subprocess.run(['systemctl','--no-block','poweroff'],check=True,timeout=10)


if __name__=='__main__':
    try:
        run(['ip','link','set','lo','up'])
        (second_boot if PHASE.exists() else first_boot)()
    except Exception as error:
        import traceback
        diagnostics={}
        for name,arguments in [('units',['systemctl','status','--no-pager','--full','pgvnserve','pgvnhttpd']),('journal',['journalctl','-b','--no-pager','-n','120','-u','pgvnserve','-u','pgvnhttpd'])]:
            result=subprocess.run(arguments,text=True,capture_output=True,timeout=15)
            diagnostics[name]=result.stdout+result.stderr
        error_log=Path('/var/log/pgvn/httpd-error.log')
        if error_log.exists():diagnostics['apache_error_log']=error_log.read_text(errors='replace')[-16000:]
        summary={'status':'FAIL','failure':traceback.format_exc(),'checks':checks,'commands':commands,'diagnostics':diagnostics}
        SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');SUMMARY.chmod(0o600)
        print('PGVN_VM_STATUS FAIL '+str(error),flush=True);os.sync();subprocess.run(['systemctl','--no-block','poweroff'],check=True,timeout=10);sys.exit(1)
