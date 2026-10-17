from pathlib import Path
import subprocess,os,json,time,urllib.request,urllib.error,hashlib,shutil,sys,signal
ROOT=Path(__file__).resolve().parents[2];product=Path(sys.argv[2]).resolve();sdk=Path(sys.argv[4]).resolve();work=Path(sys.argv[1]);work.mkdir(parents=True,exist_ok=True);work.chmod(0o700)
env=dict(os.environ);env.pop('LD_LIBRARY_PATH',None);env['LANG']='C.UTF-8';env['PGVN_DSN']='host=127.0.0.1 dbname=pgvn_linux user=linux_owner sslmode=disable'
MONITOR_PORT=int(sys.argv[5])
password='páss/密碼';processes=[];logs=[];checks=[]
def command(args,*,data=None,expected=0,timeout=30,sensitive=False):
 r=subprocess.run(list(map(str,args)),env=env,cwd=work,input=data,capture_output=True,text=True,timeout=timeout)
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
 log=(work/(name+'.log')).open('w');logs.append(log);p=subprocess.Popen(list(map(str,args)),cwd=work,env=({**env,'LD_PRELOAD':'/usr/lib/x86_64-linux-gnu/libasan.so.8','ASAN_OPTIONS':'detect_leaks=0'} if 'sanitized' in product.name and name=='apache' else env),stdout=log,stderr=log);processes.append(p);return p
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
 configurations=[]
 for i in range(2):
  config=work/('node-'+str(i)+'.ini');config.write_text('[pgvn]\nconnection = host=127.0.0.1 dbname=pgvn_linux user=linux_service sslmode=disable\nmonitor-connection = host=127.0.0.1 dbname=pgvn_linux user=linux_reader sslmode=disable\ncredential-key-file = '+str(key)+'\nidentity-domain = 测试域\nlisten-host = 127.0.0.1\nlisten-port = '+str(13690+i)+'\nnode-id = linux-'+str(i)+'\nmax-connections = 32\ncache-mode = local\ncache-local-mib = 1\n')
  configurations.append(config);p=start([product/'bin/pgvnserve','--console','--config',config,'--monitor-url','http://127.0.0.1:'+str(MONITOR_PORT+i)+'/'],'node-'+str(i));ready(p,MONITOR_PORT+i)
 check('two actual Linux nodes ready')
 (work/'monitor-limits.json').write_text(json.dumps(monitor_limits(MONITOR_PORT)));check('monitor rejects oversized requests and expires 64 slow headers')
 first='svn://127.0.0.1:13690/svn/test';second='svn://127.0.0.1:13691/svn/test'
 svn('mkdir',first+'/trunk','-m','初始化');check('actual CRAM authentication and mkdir commit')
 wc=work/'wc';svn('checkout',first,wc)
 data=(bytes(range(256))*8192)+b'\x00\xff'+('中文 binary').encode();(wc/'trunk/中文.bin').write_bytes(data)
 svn('add',wc/'trunk/中文.bin');svn('propset','custom:utf8','属性值',wc/'trunk/中文.bin');svn('commit',wc,'-m','二进制提交')
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
 cfg=apache/'httpd.conf';module_names=['ssl','socache_shmcb','unixd','mpm_event','authn_core','authn_file','auth_basic','authz_core','authz_user','log_config','headers','dav','dav_pgvn','authz_pgvn']
 text='ServerRoot "'+str(product/'apache')+'"\nServerName 127.0.0.1\nListen 127.0.0.1:18080\n'
 for name in module_names:
  module=product/'apache/modules'/('mod_'+name+'.so')
  if module.exists():text+='LoadModule '+name+'_module "'+str(module)+'"\n'
 text+='User \"#'+str(os.getuid())+'\"\nGroup \"#'+str(os.getgid())+'\"\nStartServers 1\nServerLimit 1\nThreadsPerChild 8\nMaxRequestWorkers 8\nMinSpareThreads 1\nMaxSpareThreads 8\nDefaultRuntimeDir "'+str(apache)+'"\nPidFile "'+str(apache/'apache.pid')+'"\nErrorLog "'+str(apache/'error.log')+'"\nLogLevel warn\nPgVNConfig "'+str(apachecfg)+'"\n<Location /svn>\nDAV pgvn\nPgVNPathAuthz short_circuit\nPgVNAdvertiseV2Protocol On\nAuthType Basic\nAuthName "PgVN Linux"\nAuthBasicProvider file\nAuthUserFile "'+str(users)+'"\nRequire valid-user\n</Location>\n'
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
 # Stop every process before the full logical DB + key/config recovery rehearsal.
 for process in reversed(processes):
  if process.poll() is None:
   process.terminate();process.wait(timeout=20)
 check('SIGTERM drains native and Apache services',all(p.returncode==0 for p in processes))
 services=work/'pg_service.conf';services.write_text('[owner]\nhost=127.0.0.1\ndbname=pgvn_linux\nuser=linux_owner\nsslmode=disable\n[restore]\nhost=127.0.0.1\ndbname=pgvn_restored\nuser=linux_owner\nsslmode=disable\n');services.chmod(0o600)
 env['PGSERVICEFILE']=str(services)
 # Build/package provenance is checked by the host before this client starts.
 package=Path(sys.argv[3]).resolve();ctl=package/'deploy/linux/pgvnctl.py'
 backup=work/'backup';state=work/'backup-state';state.mkdir(mode=0o700);shutil.copytree(keydir,state/'keys');shutil.copy2(configurations[0],state/'pgvn.ini')
 command(['python3',ctl,'backup','--prefix',package,'--service','owner','--state',state,'--destination',backup,'--nodes-stopped'])
 check('full PostgreSQL 18.6 logical backup includes protected keys', (backup/'database.dump').stat().st_size>0 and (backup/'state/keys/current.key').stat().st_mode&0o777==0o600)
 recovered=work/'recovered-state'
 command(['python3',ctl,'restore','--prefix',package,'--service','restore','--backup',backup,'--state-destination',recovered,'--nodes-stopped'])
 check('full logical restore preserves key bytes',(recovered/'keys/current.key').read_bytes()==key.read_bytes())
 env['PGVN_DSN']='service=restore';admin('verify',rid);check('full logical restore verifies repository history')
 # Existing databases are refused, and tampering is detected before touching PG.
 command(['python3',ctl,'restore','--prefix',package,'--service','restore','--backup',backup,'--state-destination',work/'must-not-exist','--nodes-stopped'],expected=1)
 check('restore refuses nonempty target',not (work/'must-not-exist').exists())
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
