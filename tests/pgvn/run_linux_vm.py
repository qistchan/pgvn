"""Boot a disposable Ubuntu 24.04 kernel with QEMU TCG; no KVM or NIC required.

The guest exercises the real install scripts, systemd, crash restart and reboot.
All disks, fixture credentials and logs remain in the private artifact directory.
"""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess,sys,time,traceback,uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/pgvn'))
from build_metadata import collect,sha256
from check_pair import load_package


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--hook-artifacts',type=Path,required=True)
    parser.add_argument('--previous-package',type=Path,help='compatible Debug package for upgrade/rollback rehearsal')
    parser.add_argument('--keep-disk',action='store_true')
    parser.add_argument('--tools-image',default='pgvn-linux-vm-tools:ubuntu24')
    parser.add_argument('--allow-provenance-mismatch',action='store_true',help='explicit development preflight only')
    args=parser.parse_args();package=args.package.resolve(strict=True);hooks=args.hook_artifacts.resolve(strict=True)
    identity=load_package(package);frozen=collect(ROOT,'linux',identity['configuration'])
    previous=args.previous_package.resolve(strict=True) if args.previous_package else None
    previous_identity=load_package(previous) if previous else None
    if previous_identity:
        for key in ('target_os','target_arch','schemas','migrations_sha256','git_commit','source_manifest_sha256'):
            if previous_identity[key]!=identity[key]:raise ValueError('Previous package must share the tested source and schema')
        if previous_identity['git_worktree_dirty']:raise ValueError('Previous package must come from clean source')
    matches=identity==frozen['build_identity'] and identity['git_worktree_dirty'] is False
    if not matches and not args.allow_provenance_mismatch:raise ValueError('Commit test inputs and rebuild the matching product first')
    run_id='linux-vm-'+uuid.uuid4().hex[:12];work=ROOT/'artifacts/pgvn/tests'/run_id
    work.mkdir(mode=0o700,parents=True);work.chmod(0o700)
    env=dict(os.environ,BUILDX_CONFIG='/tmp/pgvn-buildx')
    for name in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(name,None)
    docker=['docker','--host=unix:///var/run/docker.sock'];created=[]
    report={'format':1,'run_id':run_id,'status':'FAIL','preflight':not matches,'source':frozen,
            'package_identity':identity,'package_manifest_sha256':sha256(package/'package-manifest.json'),
            'previous_package_identity':previous_identity,
            'previous_package_manifest_sha256':sha256(previous/'package-manifest.json') if previous else None,
            'method':'Fresh Ubuntu root + real distro kernel, QEMU TCG, two boots, NIC disabled; no performance claim'}
    def command(arguments,timeout=180,check=True,stdout_file=None):
        arguments=list(map(str,arguments));start=time.monotonic()
        if stdout_file:
            with stdout_file.open('wb') as stream:result=subprocess.run(arguments,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.PIPE,timeout=timeout)
            output='written to '+str(stdout_file);error=result.stderr.decode(errors='replace')
        else:
            result=subprocess.run(arguments,cwd=ROOT,env=env,text=True,capture_output=True,timeout=timeout);output=result.stdout;error=result.stderr
        with (work/'commands.jsonl').open('a') as stream:stream.write(json.dumps({'args':arguments,'exit_code':result.returncode,'stdout':output,'stderr':error,'elapsed_seconds':time.monotonic()-start})+'\n')
        if check and result.returncode:raise RuntimeError('VM command failed: '+str(arguments)+'\n'+output+error)
        return output
    try:
        context=work/'context';context.mkdir();fixture=context/'fixture';fixture.mkdir();shutil.copytree(package,fixture/'package')
        if previous:shutil.copytree(previous,fixture/'previous-package')
        for name in ('linux-vm.Dockerfile','linux_vm_guest.py','pgvn-vm-acceptance.service'):
            shutil.copy2(ROOT/'tests/pgvn'/name,(context if name.endswith('.Dockerfile') else fixture)/name)
        shutil.copy2(ROOT/'scripts/pgvn/linux-dependencies.json',context/'linux-dependencies.json')
        lock=json.loads((context/'linux-dependencies.json').read_text());pg=next(x for x in lock['sources'] if x['name']=='postgresql')
        archive=ROOT/'vendor/linux-archives'/pg['archive']
        if sha256(archive)!=pg['sha256']:raise ValueError('PostgreSQL archive mismatch')
        shutil.copy2(archive,context/'postgresql.tar.gz')
        for name in ('hook-exec-probe','hook-child'):shutil.copy2(hooks/name,fixture/name)
        report['hook_probes']={name:sha256(fixture/name) for name in ('hook-exec-probe','hook-child')}
        print('Building isolated Ubuntu guest image',flush=True)
        command(docker+['buildx','build','--load','--secret','id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt',
                       '-t','pgvn-linux-vm-guest:ubuntu24','-f',context/'linux-vm.Dockerfile',context],timeout=1800)
        guest=json.loads(command(docker+['image','inspect','pgvn-linux-vm-guest:ubuntu24']))[0]['Id']
        tools=json.loads(command(docker+['image','inspect',args.tools_image]))[0]['Id']
        report['images']={'guest':guest,'tools':tools}
        name=run_id+'-export';command(docker+['create','--name',name,guest]);created.append(name)
        archive=work/'rootfs.tar';command(docker+['export',name],stdout_file=archive);archive.chmod(0o600)
        command(docker+['rm',name]);created.remove(name)
        disk=work/'disk';disk.mkdir(mode=0o700)
        populate=r"""import pathlib,tarfile,subprocess,shutil,os,sys
root=pathlib.Path('/rootfs');root.mkdir()
with tarfile.open('/input/rootfs.tar') as tar:tar.extractall(root,filter='fully_trusted')
# Exported container markers must not select systemd's container shutdown path.
for name in ('.dockerenv','run/.containerenv','run/systemd/container'):(root/name).unlink(missing_ok=True)
shutil.copytree('/input/fixture',root/'opt/fixture');(root/'opt/fixture').chmod(0o755)
for name in ('hook-exec-probe','hook-child'):(root/'opt/fixture'/name).chmod(0o755)
unit=root/'etc/systemd/system/pgvn-vm-acceptance.service'
shutil.copy2(root/'opt/fixture/pgvn-vm-acceptance.service',unit);unit.chmod(0o644)
wants=root/'etc/systemd/system/multi-user.target.wants';wants.mkdir(exist_ok=True)
(wants/unit.name).symlink_to('/etc/systemd/system/'+unit.name)
(root/'etc/machine-id').write_text('');(root/'etc/fstab').write_text('/dev/vda / ext4 defaults 0 1\n')
output=pathlib.Path('/output');image=output/'root.img'
with image.open('wb') as stream:stream.truncate(2*1024**3)
subprocess.run(['mkfs.ext4','-F','-q','-L','pgvn-root','-d',str(root),str(image)],check=True)
kernel=next((root/'boot').glob('vmlinuz-*'));initrd=root/'boot'/('initrd.img-'+kernel.name.removeprefix('vmlinuz-'))
shutil.copy2(kernel,output/'vmlinuz');shutil.copy2(initrd,output/'initrd.img')
for path in output.iterdir():os.chown(path,int(sys.argv[1]),int(sys.argv[2]));path.chmod(0o600)
"""
        command(docker+['run','--rm','--network','none','--memory','1g','--mount','type=bind,src='+str(archive)+',dst=/input/rootfs.tar,readonly',
                        '--mount','type=bind,src='+str(fixture)+',dst=/input/fixture,readonly','--mount','type=bind,src='+str(disk)+',dst=/output',tools,'python3','-c',populate,str(os.getuid()),str(os.getgid())],timeout=300)
        archive.unlink();report['export_scratch_removed']=True
        report['boot_inputs']={name:sha256(disk/name) for name in ('vmlinuz','initrd.img')}
        print('Booting actual Ubuntu kernel with QEMU TCG; serial log: '+str(disk/'serial.log'),flush=True)
        name=run_id+'-qemu';created.append(name)
        command(docker+['run','--rm','--name',name,'--init','--network','none','--user',str(os.getuid())+':'+str(os.getgid()),
            '--memory','2g','--cpus','2','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--mount','type=bind,src='+str(disk)+',dst=/evidence',tools,'qemu-system-x86_64',
            '-machine','q35','-accel','tcg,thread=multi','-cpu','max','-smp','2','-m','1024',
            '-display','none','-monitor','none','-nic','none','-serial','file:/evidence/serial.log',
            '-object','rng-random,filename=/dev/urandom,id=rng0','-device','virtio-rng-pci,rng=rng0',
            '-drive','file=/evidence/root.img,if=virtio,format=raw,cache=writeback',
            '-kernel','/evidence/vmlinuz','-initrd','/evidence/initrd.img',
            '-append','root=/dev/vda rw console=ttyS0 panic=20'],timeout=1200)
        report['filesystem_check']=command(docker+['run','--rm','--network','none','--user',str(os.getuid())+':'+str(os.getgid()),'--mount','type=bind,src='+str(disk)+',dst=/evidence,readonly',tools,'e2fsck','-f','-n','/evidence/root.img'])
        summary=command(docker+['run','--rm','--network','none','--user',str(os.getuid())+':'+str(os.getgid()),'--mount',
            'type=bind,src='+str(disk)+',dst=/evidence,readonly',tools,'debugfs','-R','cat /var/lib/pgvn-vm/summary.json','/evidence/root.img'])
        (work/'guest-summary.json').write_text(summary);report['guest']=json.loads(summary)
        if report['guest']['status']!='PASS':raise RuntimeError('Guest acceptance failed: '+report['guest'].get('failure','unknown'))
        if report['guest']['package_manifest_sha256']!=report['package_manifest_sha256']:raise ValueError('Guest tested another package')
        report['source_unchanged']=collect(ROOT,'linux',identity['configuration'])==frozen
        if not report['source_unchanged']:raise ValueError('Test inputs changed while the VM ran')
        report['status']='PASS';report['disk_sha256']=sha256(disk/'root.img')
        if not args.keep_disk:
            (disk/'root.img').unlink();report['disk_removed']=True
    except Exception:report['failure']=traceback.format_exc()
    finally:
        report['teardown']=True
        for name in reversed(created):
            try:
                command(docker+['rm','-f',name],check=False)
                state=command(docker+['inspect','--format','{{.State.Status}}',name],check=False)
                if state.strip(): report['teardown']=False
            except Exception:report['teardown']=False
        if not report['teardown']:report['status']='FAIL'
        (work/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'preflight':report['preflight'],'report':str(work/'summary.json')}),flush=True)
    if report['status']!='PASS':print(report.get('failure','teardown failed'),file=sys.stderr);return 1
    return 0


if __name__=='__main__':sys.exit(main())
