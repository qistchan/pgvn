"""Import locked historical FSFS input into an ID repository via dump/load."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import tarfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def legacy_repository(sbox,tar_filename,directory,config,snapshot,marker,create,remove,event):
    work=Path(config['work']).resolve();alias=Path(sbox.repo_dir).resolve()
    assert re.fullmatch('pgvn_m6_net_[0-9a-f]{12}',config['database'])
    assert work==(ROOT/'runtime/pgvn/tests'/('m6-network-'+config['database'][-12:])/'pgvn').resolve()
    assert alias.is_relative_to(work/'svn-test-work/repositories') and alias.parent!=work
    assert Path(tar_filename).name==tar_filename
    relative='subversion/tests/cmdline/upgrade_tests_data/'+tar_filename
    source=snapshot/relative
    assert source.resolve().is_relative_to(snapshot) and sha(source)==marker['files'][relative]['sha256']
    directory=directory or tar_filename.split('.')[0]
    assert Path(directory).name==directory and directory not in ('.','..')
    extracted=work/'global-fixture-inputs'/('legacy-'+uuid.uuid4().hex)
    extracted.mkdir(parents=True,exist_ok=False)
    # Preserve the old FSFS repository only as an import source, outside every
    # native alias. Reject path traversal and links even in a locked archive.
    with tarfile.open(source,'r:bz2') as archive:
        for member in archive.getmembers():
            assert (extracted/member.name).resolve().is_relative_to(extracted)
            assert member.isdir() or member.isfile(),'Unsupported historical fixture tar member'
        archive.extractall(extracted,filter='data')
    old=extracted/directory;assert old.is_dir()
    env=dict(os.environ)
    def run(program,arguments,input=None):
        result=subprocess.run([str(program),*map(str,arguments)],cwd=work,env=env,input=input,
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=90)
        assert result.returncode==0,(Path(program).name,result.stderr[-3000:])
        return result.stdout
    stock=ROOT/'runtime/pgvn/bin';native=ROOT/'runtime/pgvn/upstream-tools/bin'
    dump=run(stock/'svnadmin.exe',['dump','--quiet',old])
    dump_file=extracted/'repository.dump';dump_file.write_bytes(dump)
    expected={name:run(stock/'svnlook.exe',[name,old]).strip().decode('ascii') for name in ('uuid','youngest')}
    remove(sbox.repo_dir);assert not alias.exists();create(sbox.repo_dir)
    run(native/'pgvn-upstream-svnadmin.exe',['load',alias,'--force-uuid','--quiet'],input=dump)
    actual={name:run(native/'pgvn-upstream-svnlook.exe',[name,alias]).strip().decode('ascii') for name in expected}
    assert actual==expected and not alias.exists(),(expected,actual)
    event('legacy-repository-import',alias=str(alias),source=str(source),source_sha256=sha(source),
          dump=str(dump_file),dump_sha256=sha(dump_file),verified=actual,
          method='Unchanged stock svnadmin dump; native original-source svnadmin load --force-uuid.')


def install_working_copy_moves(upgrades,config,snapshot,marker,event):
    """Retry only the unchanged old-WC helper's same-volume Windows rename.

    The original helper still extracts the locked archive and removes the old
    WC. Its private shutil proxy avoids the copy/delete fallback, which cannot
    remove the read-only .svn files when a rename is temporarily denied.
    No global shutil function or upstream test/assertion is changed.
    """
    assert os.name=='nt'
    work=Path(config['work']).resolve();backend=config['backend']
    assert backend in ('fsfs','pgvn') and re.fullmatch('pgvn_m6_net_[0-9a-f]{12}',config['database'])
    assert work==(ROOT/'runtime/pgvn/tests'/('m6-network-'+config['database'][-12:])/backend).resolve()
    snapshot=Path(snapshot).resolve()
    original=upgrades.replace_sbox_with_tarfile;original_shutil=upgrades.shutil

    def inventory(root):
        records={}
        for path in [root,*sorted(root.rglob('*'))]:
            info=path.lstat()
            records[path.relative_to(root).as_posix()]={
                'mode':info.st_mode,'attributes':info.st_file_attributes,
                'link':os.readlink(path) if path.is_symlink() else None,
                'sha256':sha(path) if path.is_file() and not path.is_symlink() else None}
        return records

    def replace(sbox,tar_filename,dir=None):
        assert Path.cwd().resolve()==work and upgrades.shutil is original_shutil
        assert Path(tar_filename).name==tar_filename
        relative='subversion/tests/cmdline/upgrade_tests_data/'+tar_filename
        archive=snapshot/relative
        assert archive.resolve().is_relative_to(snapshot) and sha(archive)==marker['files'][relative]['sha256']
        directory=dir or tar_filename.split('.')[0]
        assert Path(directory).name==directory and directory not in ('.','..')
        target=Path(sbox.wc_dir).resolve()
        assert target.is_relative_to(work/'svn-test-work/working_copies')
        assert target!=work/'svn-test-work/working_copies'

        class MoveProxy:
            def __getattr__(self,name):return getattr(original_shutil,name)
            def move(self,source,destination):
                first=Path(source).resolve();second=Path(destination).resolve()
                temporary=work/'svn-test-work/local_tmp'
                assert first.parent.parent==temporary and re.fullmatch('tmp[a-z0-9_]+',first.parent.name)
                assert first.name==directory and first.is_dir() and second==target
                assert first.stat().st_dev==second.parent.stat().st_dev
                before=inventory(first);attempts=0;errors={};deadline=time.monotonic()+3
                while True:
                    # A failed original cleanup or a racing destination must
                    # remain a failure, never move the WC inside that path.
                    if os.path.lexists(second):raise FileExistsError(str(second))
                    assert first.is_dir() and first.resolve()==first
                    attempts+=1
                    try:
                        os.rename(first,second)
                        break
                    except PermissionError as error:
                        if error.winerror not in (5,32,33):raise
                        key=str(error.winerror);errors[key]=errors.get(key,0)+1
                        if time.monotonic()>=deadline:
                            event('legacy-working-copy-move-failed',source=str(first),target=str(second),
                                  attempts=attempts,windows_errors=errors)
                            raise
                        time.sleep(.01)
                assert not first.exists() and inventory(second)==before,'Historical WC bytes or attributes changed'
                fingerprint=hashlib.sha256(json.dumps(before,sort_keys=True).encode('utf-8')).hexdigest()
                event('legacy-working-copy-move',source=str(first),target=str(second),
                      archive=str(archive),archive_sha256=sha(archive),attempts=attempts,windows_errors=errors,
                      tree_sha256=fingerprint,entries=len(before),bytes_and_attributes_preserved=True)
                return str(destination)

        upgrades.shutil=MoveProxy()
        try:return original(sbox,tar_filename,dir)
        finally:upgrades.shutil=original_shutil

    upgrades.replace_sbox_with_tarfile=replace
