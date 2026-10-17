from pathlib import Path
import subprocess,os,json,shutil,struct,base64
import argparse
root=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--product',type=Path,required=True);p.add_argument('--sdk',type=Path,required=True);p.add_argument('--work',type=Path,required=True);a=p.parse_args()
product=a.product.resolve();sdk=a.sdk.resolve();work=a.work.resolve();work.mkdir(parents=True,exist_ok=False)
flags=['cc','-std=c17','-D_GNU_SOURCE','-pthread','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-O0','-g']
for path in (product/'generated',root/'src/pgvn/core',root/'src/pgvn/platform',root/'src/libsvn_fs_pg',sdk/'include/subversion-1',sdk/'include/apr-1',sdk/'include'):flags+=['-I',str(path)]
if 'sanitized' in product.name:flags+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
env=dict(os.environ);env['LD_LIBRARY_PATH']=str(product/'lib')
def run(args):
 r=subprocess.run(list(map(str,args)),env=env,text=True,capture_output=True,timeout=90)
 print(r.stdout,r.stderr,flush=True)
 if r.returncode:raise RuntimeError((args,r.returncode))
 return r.stdout
run([*flags,root/'tests/pgvn/hook_process_child.c','-o',work/'hook-child'])
run([*flags,root/'tests/pgvn/hook_exec_probe.c',product/'lib/libpgvn_core.a','-L'+str(product/'lib'),'-lsvn_fs-1','-lsvn_fs_util-1','-lsvn_diff-1','-lsvn_delta-1','-lsvn_subr-1','-laprutil-1','-lapr-1','-lpq','-lcrypto','-o',work/'hook-exec-probe'])
scratch=work/'root'
if scratch.exists():shutil.rmtree(scratch)
scratch.mkdir(mode=0o700);shutil.copy2(work/'hook-child',scratch/'probe-child');(scratch/'probe-child').chmod(0o700)
output=run([work/'hook-exec-probe',scratch,product/'bin/pgvn-hook-runner'])
record=json.loads((scratch/'record.event.json').read_text())
assert record['version']==1 and record['event']=='pre-commit' and record['username']=='用戶é' and record['revision']==42
assert record['property_value_base64']==base64.b64encode(bytes([0,255,120,10])).decode()
assert record['paths']==['/中文','/a\\b'] and record['capabilities']==['mergeinfo'] and record['force'] is True
arguments=[];data=(scratch/'record.args').read_bytes()
while data:
 size=struct.unpack('<I',data[:4])[0];arguments.append(data[4:4+size].decode());data=data[4+size:]
assert arguments==['','中文 参数','& ; $(literal)','quote " tail\\'],arguments
values=dict(line.split('=',1) for line in (scratch/'record.env').read_text().splitlines())
assert values['PGVN_DSN']==values['PGPASSWORD']==values['HOME']=='<absent>'
assert values['PGVN_USER']=='用戶é' and values['PGVN_CONFIG']=='/non-secret/reference.ini' and values['PGSERVICEFILE']=='/non-secret/services.conf'
assert (scratch/'record.fds').read_text().strip()=='0'
print(json.dumps({'independent_record_checks':'PASS','native':json.loads(output)}))

run([*flags,root/"tests/pgvn/cache_probe.c",product/"lib/libpgvn_core.a","-L"+str(product/"lib"),"-lsvn_fs-1","-lsvn_fs_util-1","-lsvn_diff-1","-lsvn_delta-1","-lsvn_subr-1","-laprutil-1","-lapr-1","-lpq","-lcrypto","-o",work/"cache-probe"])
run([work/"cache-probe"])
