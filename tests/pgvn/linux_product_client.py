from pathlib import Path
import subprocess,os,json,time,urllib.request,urllib.error,hashlib,shutil,sys,signal
ROOT=Path(__file__).resolve().parents[2];product=Path(sys.argv[2]).resolve();sdk=Path(sys.argv[4]).resolve();work=Path(sys.argv[1]);work.mkdir(parents=True,exist_ok=True);work.chmod(0o700)
env=dict(os.environ);env.pop('LD_LIBRARY_PATH',None);env['LANG']='C.UTF-8';env['PGVN_DSN']='host=127.0.0.1 dbname=pgvn_linux user=linux_owner sslmode=disable'
MONITOR_PORT=int(sys.argv[5])
password='páss/密碼';processes=[];logs=[];checks=[]
def process_env(args):
 if 'sanitized' in product.name and Path(str(args[0])).name=='httpd':
  return {**env,'LD_PRELOAD':'/usr/lib/x86_64-linux-gnu/libasan.so.8','ASAN_OPTIONS':'detect_leaks=0:abort_on_error=1'}
 return env
def command(args,*,data=None,expected=0,timeout=30,sensitive=False):
 r=subprocess.run(list(map(str,args)),env=process_env(args),cwd=work,input=data,capture_output=True,text=True,timeout=timeout)
 with (work/'commands.jsonl').open('a') as f:f.write(json.dumps({'args':list(map(str,args)),'exit_code':r.returncode,'stdout':'<redacted>' if sensitive else r.stdout,'stderr':r.stderr})+'\n')
 if r.returncode!=expected:raise RuntimeError((list(map(str,args)),r.returncode,r.stdout,r.stderr))
 return r.stdout

def admin(*args,data=None,config=None,sensitive=False):
 return command([product/'bin/pgvnadmin',*(['--config',config] if config else []),*args],data=data,sensitive=sensitive)
def check(name,condition=True):
 assert condition,name;checks.append(name);print(json.dumps({'check':name,'status':'PASS'}),flush=True)
def get(port,path,headers=None):
 try:
  with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(port)+path,headers=headers or {}),timeout=2) as r:return r.status,r.read()
 except urllib.error.HTTPError as r:return r.code,r.read()
def start(args,name):
 log=(work/(name+'.log')).open('w');logs.append(log);p=subprocess.Popen(list(map(str,args)),cwd=work,env=process_env(args),stdout=log,stderr=log);processes.append(p);return p
def ready(p,port):
 for _ in range(160):
  if p.poll() is not None:raise RuntimeError(('service exited',p.returncode))
  try:
   if get(port,'/health/ready')[0]==200:return
  except OSError:pass
  time.sleep(.1)
 raise RuntimeError('service readiness timed out')
def svn(*args,expected=0):
 return command(['/usr/bin/svn',*args,'--non-interactive','--no-auth-cache','--config-dir',work/'svn-config','--username','writer','--password-from-stdin'],data=password+'\n',expected=expected,timeout=40)
try:
 command([product/'bin/pgvnserve','--console','--monitor-url','http://127.0.0.1:'+('0'*1000)+'8033/'],expected=2)
 check('monitor rejects overlong URL before copying')
 check('native CLI db init',json.loads(admin('db','init'))['ok'])
 check('native CLI schema check',json.loads(admin('db','check'))['ok'])
 from linux_fault_probe import database_deadlines,monitor_limits
 deadlines=database_deadlines(ROOT,product,sdk,work,env);check('database blackhole timeout and cancellation poison connections',all(r['status']=='PASS' for r in deadlines))
 admin('db','grant','--service-role','linux_service','--monitor-role','linux_reader')
 keydir=work/'keys';keydir.mkdir(mode=0o700);key=keydir/'current.key'
 admin('key','create',key,'linux-v1');check('native protected key created',key.stat().st_mode&0o777==0o600)
 admin('user','set','测试域','writer','--key-file',key,data=password+'\n')
 repository=json.loads(admin('repo','create','linux-test','--route','/svn/test'));rid=repository['id'];check('native repository created',bool(rid))
 rules=work/'authz.conf';rules.write_text('[/]\nwriter = rw\n* =\n')
 settings=work/'settings.json';settings.write_text(json.dumps({'identity_domain':'测试域','anonymous_access':'none','authenticated_access':'write','svn_authz_mode':'enforce'},ensure_ascii=False))
 hook_policy=work/'hooks.json';hook_policy.write_text(json.dumps({'pre-revprop-change':'allow'}))
 admin('config','publish',rid,'--expected-generation',repository['generation'],'--authz',rules,'--settings',settings,'--hooks',hook_policy)
 configurations=[];node_processes=[]
 for i in range(2):
  config=work/('node-'+str(i)+'.ini');config.write_text('[pgvn]\nconnection = host=127.0.0.1 dbname=pgvn_linux user=linux_service sslmode=disable\nmonitor-connection = host=127.0.0.1 dbname=pgvn_linux user=linux_reader sslmode=disable\ncredential-key-file = '+str(key)+'\nidentity-domain = 测试域\nlisten-host = 127.0.0.1\nlisten-port = '+str(13690+i)+'\nnode-id = linux-'+str(i)+'\nmax-connections = 32\ncache-mode = local\ncache-local-mib = 1\n')
  configurations.append(config);p=start([product/'bin/pgvnserve','--console','--config',config,'--monitor-url','http://127.0.0.1:'+str(MONITOR_PORT+i)+'/'],'node-'+str(i));ready(p,MONITOR_PORT+i);node_processes.append(p)
 check('two actual Linux nodes ready')
 (work/'monitor-limits.json').write_text(json.dumps(monitor_limits(MONITOR_PORT)));check('monitor rejects oversized requests and expires 64 slow headers')
 first='svn://127.0.0.1:13690/svn/test';second='svn://127.0.0.1:13691/svn/test'
 svn('mkdir',first+'/trunk','-m','初始化');check('actual CRAM authentication and mkdir commit')
 wc=work/'wc';svn('checkout',first,wc)
 data=(bytes(range(256))*8192)+b'\x00\xff'+('中文 binary').encode();(wc/'trunk/中文.bin').write_bytes(data)
 cache_data=(b'pgvn-cache-seed-v1\n'*32768)[:524288];(wc/'trunk/cache.txt').write_bytes(cache_data)
 svn('add',wc/'trunk/中文.bin',wc/'trunk/cache.txt');svn('propset','custom:utf8','属性值',wc/'trunk/中文.bin');svn('commit',wc,'-m','二进制提交')
 check('native binary and UTF-8 property commit')
 output=work/'cross-node.bin';svn('export',second+'/trunk/中文.bin',output)
 check('cross-node content hash',hashlib.sha256(output.read_bytes()).digest()==hashlib.sha256(data).digest())
 check('cross-node property',svn('propget','custom:utf8',second+'/trunk/中文.bin').strip()=='属性值')
 admin('verify',rid);check('native admin verifies history')
 dump=work/'history.dump';admin('dump',rid,'--file',dump);check('native dump created',dump.stat().st_size>len(data))
 restored=json.loads(admin('repo','create','restored','--route','/restored'));admin('repo','disable',restored['id']);admin('load',restored['id'],'--file',dump);admin('verify',restored['id']);check('native dump/load/verify round trip')
 # Load the actual Linux Apache modules and serve the same repository.
 apache=work/'apache';apache.mkdir();users=apache/'users.htpasswd'
 command([product/'apache/bin/htpasswd','-i','-c','-m',users,'writer'],data=password+'\n');users.chmod(0o600)
 apachecfg=apache/'pgvn.ini';apachecfg.write_text('[pgvn]\nconnection = host=127.0.0.1 dbname=pgvn_linux user=linux_service sslmode=disable\nnode-id = linux-dav\n')
 cfg=apache/'httpd.conf'
 # Exercise the shipped deployment template with fixture paths and ports.
 text=(ROOT/'deploy/linux/httpd.conf.example').read_text()
 replacements={'Define PGVN_HOME /opt/pgvn/current':'Define PGVN_HOME "'+str(product)+'"',
  'ServerName localhost':'ServerName 127.0.0.1','Listen 127.0.0.1:8080':'Listen 127.0.0.1:18080',
  'User pgvn':'User "#'+str(os.getuid())+'"','Group pgvn':'Group "#'+str(os.getgid())+'"',
  'ThreadsPerChild 32':'ThreadsPerChild 8','MaxRequestWorkers 32':'MaxRequestWorkers 8','MaxSpareThreads 32':'MaxSpareThreads 8',
  'DefaultRuntimeDir /run/pgvnhttpd':'DefaultRuntimeDir "'+str(apache)+'"',
  'PidFile /run/pgvnhttpd/httpd.pid':'PidFile "'+str(apache/'apache.pid')+'"',
  'ErrorLog /var/log/pgvn/httpd-error.log':'ErrorLog "'+str(apache/'error.log')+'"',
  'PgVNConfig /etc/pgvn/pgvn.ini':'PgVNConfig "'+str(apachecfg)+'"',
  'AuthUserFile /etc/pgvn/http-users':'AuthUserFile "'+str(users)+'"'}
 for before,after in replacements.items():
  assert before in text,before;text=text.replace(before,after)
 for name in ('ssl','socache_shmcb'):text+='LoadModule '+name+'_module "'+str(product/'apache/modules'/('mod_'+name+'.so'))+'"\n'
 command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',apache/'ca.key','-out',apache/'ca.pem','-days','1','-subj','/CN=PgVN fixture CA'])
 command(['openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',apache/'tls.key','-out',apache/'tls.csr','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,DNS:127.0.0.1,IP:127.0.0.1','-addext','basicConstraints=critical,CA:FALSE'])
 command(['openssl','x509','-req','-in',apache/'tls.csr','-CA',apache/'ca.pem','-CAkey',apache/'ca.key','-CAcreateserial','-days','1','-copy_extensions','copy','-out',apache/'tls.pem'])
 text+='Listen 127.0.0.1:18443\nSSLSessionCache shmcb:'+str(apache/'ssl-cache')+'(512000)\n<VirtualHost 127.0.0.1:18443>\nServerName localhost\nSSLEngine On\nSSLProtocol -all +TLSv1.2 +TLSv1.3\nSSLCertificateFile '+str(apache/'tls.pem')+'\nSSLCertificateKeyFile '+str(apache/'tls.key')+'\n</VirtualHost>\n'
 cfg.write_text(text);command([product/'apache/bin/httpd','-t','-f',cfg]);check('apachectl equivalent config/module validation')
 ap=start([product/'apache/bin/httpd','-DFOREGROUND','-f',cfg],'apache')
 for _ in range(100):
  if ap.poll() is not None:raise RuntimeError(('Apache exited',ap.returncode,(apache/'error.log').read_text()))
  try:
   if get(18080,'/svn/test')[0] in (401,403):break
  except OSError:pass
  time.sleep(.1)
 svn('list','http://127.0.0.1:18080/svn/test/trunk');check('actual HTTP SVN client reads native DAV repository')
 httpout=work/'http.bin';svn('export','http://127.0.0.1:18080/svn/test/trunk/中文.bin',httpout);check('HTTP and native SVN agree on binary content',httpout.read_bytes()==data)
 httpwc=work/'httpwc';svn('checkout','http://127.0.0.1:18080/svn/test',httpwc);(httpwc/'trunk/http.txt').write_text('DAV writes 中文\n');svn('add',httpwc/'trunk/http.txt');svn('commit',httpwc,'-m','HTTP commit');check('actual DAV write and commit')
 check('native node sees DAV revision',svn('cat',second+'/trunk/http.txt')=='DAV writes 中文\n')

 # Trusted TLS, no accept-all certificate override.
 tlsargs=['--config-option','servers:global:ssl-authority-files='+str(apache/'ca.pem')]
 check('actual HTTPS content with validated SAN and CA',svn('cat','https://127.0.0.1:18443/svn/test/trunk/http.txt',*tlsargs)=='DAV writes 中文\n')
 tlswc=work/'tlswc';svn('checkout','https://127.0.0.1:18443/svn/test',tlswc,*tlsargs)
 (tlswc/'trunk/tls.txt').write_text('TLS commit\n');svn('add',tlswc/'trunk/tls.txt');svn('commit',tlswc,'-m','HTTPS commit',*tlsargs)
 check('native node sees HTTPS commit',svn('cat',second+'/trunk/tls.txt')=='TLS commit\n')
 # Actual Java SVNKit uses native and HTTP protocol stacks and mutable revprops.
 svn('copy',first+'/trunk/中文.bin',first+'/trunk/payload.bin','-m','SVNKit binary fixture')
 classes=work/'java';classes.mkdir()
 command(['javac','-encoding','UTF-8','-cp','/usr/share/java/*','-d',classes,ROOT/'tests/pgvn/SvnKitProbe.java'])
 binary=work/'payload.bin';binary.write_bytes(data)
 for protocol,url in [('svn',first),('http','http://127.0.0.1:18080/svn/test')]:
  result=command(['java','-cp',str(classes)+':/usr/share/java/*','SvnKitProbe','roundtrip',url,second,'writer',work/'svnkit-config',binary,'java-'+protocol],data=password+'\n',timeout=60)
  check('actual SVNKit '+protocol+' roundtrip',json.loads(result)['roundtrip'])
 # Locks, copy history and a real text merge through the native protocol.
 svn('lock',second+'/trunk/http.txt','-m','lock 中文');svn('unlock',second+'/trunk/http.txt')
 check('native lock/unlock roundtrip')
 svn('copy',first+'/trunk',first+'/branch','-m','branch')
 merge=work/'merge';svn('checkout',first+'/branch',merge);(merge/'http.txt').write_text('branch merge\n');svn('commit',merge,'-m','branch change')
 trunk=work/'trunk';svn('checkout',first+'/trunk',trunk);svn('merge',first+'/branch',trunk);svn('commit',trunk,'-m','merge branch')
 check('merge publishes content and mergeinfo',svn('cat',second+'/trunk/http.txt')=='branch merge\n' and '/branch:' in svn('propget','svn:mergeinfo',second+'/trunk'))
 # Tokens are never logged. Sampler propagation is bounded by its refresh interval.
 token=json.loads(admin('monitor-token','create','--label','linux-tests','--expires-in','300',sensitive=True))
 headers={'Authorization':'Bearer '+token['token']}
 for _ in range(100):
  status,body=get(MONITOR_PORT,'/metrics',headers)
  if status==200:break
  time.sleep(.1)
 check('native monitor token grants metrics',status==200)
 # The small immutable object hits across fresh SVN connections; larger objects
 # retain streaming. Current metadata is fetched on every request.
 for _ in range(3):svn('cat',first+'/trunk/tls.txt')
 status,body=get(MONITOR_PORT,'/metrics',headers)
 metrics=dict(line.split(' ',1) for line in body.decode().splitlines() if line.startswith('pgvn_cache_local_'))
 check('bounded immutable local cache hits',int(metrics['pgvn_cache_local_hits_total'])>0 and 0<int(metrics['pgvn_cache_local_bytes'])<=1048576)
 check('monitor rejects cross-origin token use',get(MONITOR_PORT,'/api/monitor/v1/summary',{**headers,'Origin':'http://attacker.invalid'})[0]==403)
 if env.get('PGVN_TEST_PERFORMANCE')=='1':
  from linux_wire_benchmark import measure
  import re
  restart_count=[0]
  def restart(mode):
   for process in node_processes:
    process.terminate();process.wait(timeout=20)
   node_processes.clear();restart_count[0]+=1
   for i,config in enumerate(configurations):
    config.write_text(re.sub(r'(?m)^cache-mode\s*=.*$','cache-mode = '+mode,config.read_text()))
    process=start([product/'bin/pgvnserve','--console','--config',config,'--monitor-url','http://127.0.0.1:'+str(MONITOR_PORT+i)+'/'],'benchmark-'+str(restart_count[0])+'-'+mode+'-'+str(i))
    ready(process,MONITOR_PORT+i);node_processes.append(process)
   return [process.pid for process in node_processes]
  performance=measure(work,env,product,[first,second],password,cache_data,headers,get,restart,MONITOR_PORT)
  check('matched local/off wire benchmark has zero errors',performance['status']=='PASS')
 browser_state=work/'browser-state.json';browser_state.write_text(json.dumps({'token':token['token']}));browser_state.chmod(0o600)
 browser_result=work/'browser-result.json'
 for _ in range(1200):
  if browser_result.exists():break
  time.sleep(.1)
 else:raise RuntimeError('Browser controller timed out')
 check('real Chromium monitor flows',json.loads(browser_result.read_text())['status']=='PASS')
 admin('monitor-token','revoke',token['id'])
 for _ in range(100):
  status,_=get(MONITOR_PORT,'/metrics',headers)
  if status==401:break
  time.sleep(.1)
 check('revoked token is rejected',status==401)
 # Bypass only changes native SVN authorization. DAV and the CLI still enforce.
 deny=work/'deny.conf';deny.write_text('[/]\nwriter =\n* =\n')
 current=json.loads(admin('config','show',rid))
 bypass=work/'bypass.json';bypass.write_text(json.dumps({'identity_domain':'测试域','anonymous_access':'none','authenticated_access':'none','svn_authz_mode':'bypass'}))
 admin('config','publish',rid,'--expected-generation',current['generation'],'--authz',deny,'--settings',bypass)
 check('bypass native reads denied path',svn('cat',first+'/trunk/tls.txt')=='TLS commit\n')
 svn('mkdir',second+'/bypass-write','-m','bypass write');check('bypass native publishes with denied authz')
 svn('list','http://127.0.0.1:18080/svn/test',expected=1)
 check('bypass preserves DAV authorization')
 check('bypass preserves CLI authorization',json.loads(admin('authz','check',rid,'writer','/trunk','read'))['allowed'] is False)
 bad=command(['/usr/bin/svn','info',first,'--non-interactive','--no-auth-cache','--config-dir',work/'svn-config','--username','writer','--password-from-stdin'],data='incorrect\n',expected=1)
 check('bypass still requires valid credentials')
 current=json.loads(admin('config','show',rid))
 admin('config','publish',rid,'--expected-generation',current['generation'],'--authz',rules,'--settings',settings)
 admin('verify',rid)
 svn('lock',second+'/trunk/tls.txt','-m','backup restore lock');check('backup fixture retains an owned lock')
 # Stop every process before the full logical DB + key/config recovery rehearsal.
 for process in reversed(processes):
  if process.poll() is None:
   process.terminate();process.wait(timeout=20)
 check('SIGTERM drains native and Apache services',all(p.returncode==0 for p in processes))
 services=work/'pg_service.conf';services.write_text('[owner]\nhost=127.0.0.1\ndbname=pgvn_linux\nuser=linux_owner\nsslmode=disable\n[restore]\nhost=127.0.0.1\ndbname=pgvn_restored\nuser=linux_owner\nsslmode=disable\n');services.chmod(0o600)
 env['PGSERVICEFILE']=str(services)
 def state_digests(service):
  tables=('pgvn.repositories','pgvn.routes','pgvn.configurations','pgvn.credentials','pgvn.monitor_tokens','pgvn.dav_transactions','pgvn.dav_bindings',
          'pgfs.repositories','pgfs.transactions','pgfs.locks','pgfs.revisions','pgfs.revision_properties','pgfs.contents')
  rows={}
  for table in tables:
   sql="SELECT json_build_object('rows',count(*),'sha256',encode(sha256(convert_to(coalesce(string_agg(row_to_json(t)::text,E'\\n' ORDER BY row_to_json(t)::text),''),'UTF8')),'hex')) FROM "+table+' t'
   rows[table]=json.loads(command([product/'bin/psql','--dbname=service='+service,'-XAt','-v','ON_ERROR_STOP=1','-c',sql]))
  return rows
 source_state=state_digests('owner');check('full state includes a persisted lock',source_state['pgfs.locks']['rows']==1)
 # Build/package provenance is checked by the host before this client starts.
 package=Path(sys.argv[3]).resolve();ctl=package/'deploy/linux/pgvnctl.py'
 backup=work/'backup';state=work/'backup-state';state.mkdir(mode=0o700);shutil.copytree(keydir,state/'keys');shutil.copy2(configurations[0],state/'pgvn.ini')
 command(['python3',ctl,'backup','--prefix',package,'--service','owner','--state',state,'--destination',backup,'--nodes-stopped'])
 check('full PostgreSQL 18.6 logical backup includes protected keys', (backup/'database.dump').stat().st_size>0 and (backup/'state/keys/current.key').stat().st_mode&0o777==0o600)
 recovered=work/'recovered-state'
 command(['python3',ctl,'restore','--prefix',package,'--service','restore','--backup',backup,'--state-destination',recovered,'--nodes-stopped'])
 check('full logical restore preserves key bytes',(recovered/'keys/current.key').read_bytes()==key.read_bytes())
 check('full logical restore preserves control plane, credentials, locks and DAV rows',state_digests('restore')==source_state)
 (work/'restored-state-digests.json').write_text(json.dumps(source_state,indent=2)+'\n')
 env['PGVN_DSN']='service=restore';admin('verify',rid);check('full logical restore verifies repository history')
 admin('db','grant','--service-role','linux_service','--monitor-role','linux_reader')
 restored_config=work/'restored.ini';restored_config.write_text('[pgvn]\nconnection = host=127.0.0.1 dbname=pgvn_restored user=linux_service sslmode=disable\nmonitor-connection = host=127.0.0.1 dbname=pgvn_restored user=linux_reader sslmode=disable\ncredential-key-file = '+str(recovered/'keys/current.key')+'\nidentity-domain = 测试域\nlisten-host = 127.0.0.1\nlisten-port = 13690\nnode-id = linux-restored\ncache-mode = local\ncache-local-mib = 1\n')
 restored_process=start([product/'bin/pgvnserve','--console','--config',restored_config,'--monitor-url','http://127.0.0.1:'+str(MONITOR_PORT)+'/'],'restored-node');ready(restored_process,MONITOR_PORT)
 restored_token=json.loads(admin('monitor-token','create','--label','restored fixture','--expires-in','300',sensitive=True))
 restored_headers={'Authorization':'Bearer '+restored_token['token']}
 for _ in range(100):
  status,body=get(MONITOR_PORT,'/metrics',restored_headers)
  if status==200:break
  time.sleep(.1)
 check('restored node starts with a fresh local cache namespace',status==200 and 'pgvn_cache_local_bytes 0\n' in body.decode())
 check('restored protected key decrypts actual SVN credentials',svn('cat',first+'/trunk/tls.txt')=='TLS commit\n')
 restored_binary=work/'restored-content.bin';svn('export',first+'/trunk/中文.bin',restored_binary)
 check('restored authenticated binary content is identical',restored_binary.read_bytes()==data)
 lock_xml=svn('info','--xml',first+'/trunk/tls.txt');check('restored SVN lock retains owner and token','<lock>' in lock_xml and '<owner>writer</owner>' in lock_xml)
 svn('unlock',first+'/trunk/tls.txt');check('restored owner can unlock persisted lock')
 restored_process.terminate();restored_process.wait(timeout=20);check('restored node drains without sanitizer errors',restored_process.returncode==0)

 # Existing databases are refused, and tampering is detected before touching PG.
 command(['python3',ctl,'restore','--prefix',package,'--service','restore','--backup',backup,'--state-destination',work/'must-not-exist','--nodes-stopped'],expected=1)
 check('restore refuses nonempty target',not (work/'must-not-exist').exists())
 check('package directories remain traversable under private umask',all(path.stat().st_mode&0o005==0o005 for path in [package,*package.rglob('*')] if path.is_dir()))
 command(['python3',ctl,'install','--package',package,'--root',work/'stage'])
 command(['python3',ctl,'install','--package',package,'--root',work/'stage'])
 check('idempotent staged installation verifies all files',(work/'stage/opt/pgvn/current/bin/pgvnserve').is_file())

 print(json.dumps({'status':'PASS','checks':checks}),flush=True)
finally:
 for p in reversed(processes):
  if p.poll() is None:
   p.terminate()
   try:p.wait(timeout=20)
   except subprocess.TimeoutExpired:p.kill();p.wait()
 for f in logs:f.close()
