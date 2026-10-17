"""Build pinned PostgreSQL backup/restore tools in an isolated Linux prefix."""
from pathlib import Path
import argparse,json,os,subprocess,tarfile,hashlib
ROOT=Path(__file__).resolve().parents[2]
def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sdk',type=Path,required=True);p.add_argument('--output',type=Path,default=ROOT/'build/pgvn/linux-tools-sdk');p.add_argument('--jobs',type=int,default=4);a=p.parse_args()
    sdk=a.sdk.resolve(strict=True);manifest=sdk/'sdk-manifest.json';base=json.loads(manifest.read_text())
    for name,expected in base['files'].items():
        if digest(sdk/name)!=expected:raise ValueError('FS SDK file mismatch: '+name)
    lock=next(s for s in json.loads((ROOT/'scripts/pgvn/linux-dependencies.json').read_text())['sources'] if s['name']=='postgresql')
    archive=ROOT/'vendor/linux-archives'/lock['archive']
    if digest(archive)!=lock['sha256']:raise ValueError('PostgreSQL source archive mismatch')
    if not 1<=a.jobs<=64:raise ValueError('jobs outside 1..64')
    prefix=a.output.resolve();inputs={'sdk_manifest_sha256':digest(manifest),'script_sha256':digest(Path(__file__)),'source':lock};marker=prefix/'.pgvn-tools-inputs.json'
    if prefix.exists() and any(prefix.iterdir()) and (not marker.is_file() or json.loads(marker.read_text())!=inputs):raise ValueError('Choose another output for changed tool inputs')
    prefix.mkdir(parents=True,exist_ok=True);marker.write_text(json.dumps(inputs,indent=2)+'\n');source=prefix/'source'
    if not source.exists():
        extraction=prefix/'extract';extraction.mkdir()
        with tarfile.open(archive) as package:package.extractall(extraction,filter='data')
        roots=list(extraction.iterdir())
        if len(roots)!=1:raise ValueError('Unexpected PostgreSQL source archive')
        roots[0].rename(source)
    env=dict(os.environ);env['LD_LIBRARY_PATH']=str(sdk/'lib');env['LDFLAGS']='-Wl,-rpath,'+str(sdk/'lib')
    def run(args):
        print(json.dumps({'args':list(map(str,args))}),flush=True)
        with (prefix/'commands.log').open('a') as log:subprocess.run(list(map(str,args)),cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
    if not (source/'config.status').exists():run(['./configure','--prefix='+str(prefix),'--without-icu','--without-readline','--with-ssl=openssl'])
    run(['make','-j'+str(a.jobs),'-C','src/backend','generated-headers'])
    for directory in ('pg_dump','psql','scripts'):
        run(['make','-j'+str(a.jobs),'-C','src/bin/'+directory]);run(['make','-C','src/bin/'+directory,'install'])
    result={'format':1,'inputs':inputs,'files':{f.relative_to(prefix).as_posix():digest(f) for f in sorted((prefix/'bin').iterdir()) if f.is_file()},'versions':{name:subprocess.check_output([prefix/'bin'/name,'--version'],text=True,env=env).strip() for name in ('pg_dump','pg_restore','psql','pg_isready')}}
    (prefix/'tools-manifest.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'output':str(prefix),'manifest_sha256':digest(prefix/'tools-manifest.json')}))
if __name__=='__main__':main()
