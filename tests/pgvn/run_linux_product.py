"""Exercise actual Linux products in isolated Docker namespaces; no host ports.

This does not represent real-host systemd/reboot or Windows acceptance.
"""
from pathlib import Path
import argparse,datetime,json,os,subprocess,sys,time,traceback,uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/pgvn'))
from build_metadata import collect,sha256
from package_linux import package


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--product',type=Path,required=True)
 parser.add_argument('--performance',action='store_true')
 parser.add_argument('--sdk',type=Path,required=True)
 parser.add_argument('--builder-image',default='pgvn-linux-product-builder:ubuntu24')
 parser.add_argument('--postgres-image',default='postgres:18.6')
 options=parser.parse_args();product=options.product.resolve(strict=True);sdk=options.sdk.resolve(strict=True)
 build=json.loads((product/'build-manifest.json').read_text());frozen=collect(ROOT,'linux',build['build_identity']['configuration'])
 if any(build[k]!=frozen[k] for k in frozen):raise ValueError('Build inputs changed; rebuild before integration testing')
 run_id='linux-product-'+uuid.uuid4().hex[:12];work=ROOT/'artifacts/pgvn/tests'/run_id
 work.mkdir(parents=True,mode=0o700);work.chmod(0o700);env=dict(os.environ)
 for name in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(name,None)
 docker=['docker','--host=unix:///var/run/docker.sock'];created=False
 report={'format':1,'run':run_id,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'FAIL','source':frozen,'product_manifest_sha256':sha256(product/'build-manifest.json'),'limits':['isolated containers','systemd/reboot requires real host','Windows deferred by user']}
 def command(args,data=None,timeout=120,check=True):
  result=subprocess.run(list(map(str,args)),cwd=ROOT,env=env,input=data,text=True,capture_output=True,timeout=timeout)
  with (work/'commands.jsonl').open('a') as stream:stream.write(json.dumps({'args':list(map(str,args)),'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})+'\n')
  if check and result.returncode:raise RuntimeError('Integration command failed: '+str(args)+'\n'+result.stdout+result.stderr)
  return result
 def client(args,timeout=300):
  return command(docker+['run','--rm','--init','--network','container:'+run_id,'--user',str(os.getuid())+':'+str(os.getgid()),'--memory','1g','--pids-limit','512','--mount','type=bind,src='+str(ROOT)+',dst='+str(ROOT),'-w',ROOT,options.builder_image,*args],timeout=timeout).stdout
 try:
  report['images']={name:json.loads(command(docker+['image','inspect',name]).stdout)[0]['Id'] for name in (options.builder_image,options.postgres_image)}
  packaged=package(product,work/'package',allow_dirty=True);report['package']=packaged
  command(docker+['run','-d','--name',run_id,'--label','pgvn.component=linux-product','--network','none','--read-only','--memory','384m','--cpus','2','--tmpfs','/var/lib/postgresql:rw,nosuid,noexec,size=384m','--tmpfs','/var/run/postgresql:rw,nosuid,noexec,size=16m','--tmpfs','/tmp:rw,nosuid,noexec,size=16m','-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_DB=pgvn_linux',options.postgres_image,'postgres','-c','shared_buffers=32MB','-c','max_connections=64']);created=True
  for _ in range(200):
   if command(docker+['exec',run_id,'pg_isready','-h','127.0.0.1','-U','postgres'],check=False).returncode==0:break
   time.sleep(.1)
  else:raise RuntimeError('PostgreSQL readiness timed out')
  command(docker+['exec','-i',run_id,'psql','-h','127.0.0.1','-X','-v','ON_ERROR_STOP=1','-U','postgres','-d','pgvn_linux'],data='CREATE ROLE linux_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; GRANT CREATE ON DATABASE pgvn_linux TO linux_owner; CREATE ROLE linux_service LOGIN NOSUPERUSER; CREATE ROLE linux_reader LOGIN NOSUPERUSER; CREATE DATABASE pgvn_restored OWNER linux_owner;')
  report['hook_output']=client(['python3',ROOT/'tests/pgvn/linux_hook_process.py','--product',product,'--sdk',sdk,'--work',work/'hooks'],timeout=180)
  print('Native Hook execution PASS',flush=True)
  from linux_browser import execute
  performance_env=['-e','PGVN_TEST_PERFORMANCE=1'] if options.performance else []
  browser_args=docker+['run','--rm','--name',run_id+'-client','--init','--network','container:'+run_id,'--user',str(os.getuid())+':'+str(os.getgid()),'--memory','1g','--pids-limit','512','--mount','type=bind,src='+str(ROOT)+',dst='+str(ROOT),'-w',ROOT,*performance_env,options.builder_image,'python3',ROOT/'tests/pgvn/linux_product_client.py',work/'client',product,packaged['directory'],sdk]
  output,report['browser']=execute(browser_args,docker,run_id+'-client',work,env,ROOT)
  (work/'client-output.jsonl').write_text(output);report['integration']=json.loads(output.splitlines()[-1])
  if options.performance:report['performance']=json.loads((work/'client/wire-benchmark.json').read_text())
  report['source_unchanged']=collect(ROOT,'linux',build['build_identity']['configuration'])==frozen
  if not report['source_unchanged']:raise ValueError('Build inputs changed during testing')
  report['status']='PASS'
 except Exception:report['failure']=traceback.format_exc()
 finally:
  command(docker+['rm','-f',run_id+'-client'],check=False)
  if created:
   (work/'postgres.log').write_text(command(docker+['logs',run_id],check=False).stdout)
   report['teardown']=command(docker+['rm','-f',run_id],check=False).returncode==0
  else:report['teardown']=True
  report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
  if not report['teardown']:report['status']='FAIL'
  (work/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({'status':report['status'],'report':str(work/'summary.json')}))
 if report['status']!='PASS':print(report.get('failure','teardown failed'),file=sys.stderr);return 1
 return 0
if __name__=='__main__':sys.exit(main())
