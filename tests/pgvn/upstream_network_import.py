"""Import locked historical FSFS input into an ID repository via dump/load."""
from pathlib import Path
import hashlib
import os
import re
import subprocess
import tarfile
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
