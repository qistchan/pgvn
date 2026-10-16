"""Prepare original log-suite inputs without native repository directories.

Only a registered repository's log-msg input and its SVN file argument move.
Historical copy setup passes through a real compatible FSFS repository. Test
functions, command results and expected log/XML output remain unchanged.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


class LogFixtures:
    def __init__(self,config,work,repositories,declared,event):
        work=Path(work).resolve()
        match=re.fullmatch(r'pgvn_m6_net_([0-9a-f]{12})',config['database']);assert match
        suffix=match[1]
        assert config['backend']=='pgvn' and config['suite']=='log_tests'
        assert config['identity_domain']=='upstream-'+suffix
        assert work==Path(config['work']).resolve()==(ROOT/'runtime/pgvn/tests'/('m6-network-'+suffix)/'pgvn').resolve()
        assert work.is_dir()
        plan_path=work.parent/'plan.json';plan=json.loads(plan_path.read_bytes())
        started=json.loads((work.parent/'started.json').read_bytes())
        assert started['plan_sha256']==sha(plan_path),'Changed or unstarted single-use plan'
        assert plan['test_id']=='m6-network-'+suffix and plan['work']==work.parent.relative_to(ROOT).as_posix()
        assert plan['suite']==config['suite'] and plan['cases']==config['cases']
        assert plan['database']==config['database'] and plan['owner']=='pgvn_migrate_'+suffix
        assert plan['endpoint']=='127.0.0.1:16432'
        assert Path(plan['cluster_data_directory']).resolve()==(ROOT/'runtime/pgvn/pg-primary').resolve()
        manifest_path=ROOT/'artifacts/pgvn/build/manifest.json'
        assert sha(manifest_path)==config['manifest_sha256']==plan['inputs']['manifest_sha256']
        self.manifest=json.loads(manifest_path.read_bytes())
        assert sha(Path(__file__))==self.manifest['sources']['tests/pgvn/upstream_network_log.py']
        self.work=work;self.config=dict(config);self.repositories=repositories
        self.declared=declared;self.event=event;self.inputs={}

    @staticmethod
    def path(value):
        return Path(os.path.abspath(os.fsdecode(os.fspath(value))))

    def repository(self,alias):
        identifier=self.repositories[alias]
        assert isinstance(identifier,str) and str(uuid.UUID(identifier))==identifier
        assert alias.is_relative_to(self.work) and alias!=self.work
        assert alias.resolve()==alias and not alias.exists(),'Native repository alias materialized'
        return identifier

    def input(self,value):
        if isinstance(value,int):return None
        try:path=self.path(value)
        except (TypeError,ValueError):return None
        if path.name!='log-msg' or path.parent not in self.repositories:return None
        assert path.parent.parent==self.work/'svn-test-work/repositories'
        identifier=self.repository(path.parent)
        base=self.work/'global-client-input'
        target=(base/identifier/'log-msg').resolve()
        assert target.is_relative_to(base) and target.parent.parent==base
        return path,identifier,target

    def write(self,original,path,contents,*args,**kwargs):
        value=self.input(path)
        if value is None:return original(path,contents,*args,**kwargs)
        alias,identifier,target=value;target.parent.mkdir(parents=True,exist_ok=True)
        result=original(str(target),contents,*args,**kwargs)
        data=target.read_bytes()
        record={'repository_id':identifier,'target':str(target),'bytes':len(data),'sha256':sha(target)}
        self.inputs[alias]=record
        self.event('client-input-write',alias=str(alias),**record)
        return result

    def command(self,command,svn_binary):
        assert str(svn_binary)==str(ROOT/'runtime/pgvn/bin/svn.exe')
        if not command or str(command[0])!=str(svn_binary):return command
        changed=None
        for index in range(1,len(command)-1):
            if command[index] not in ('-F','--file'):continue
            value=self.input(command[index+1])
            if value is None:continue
            alias,identifier,target=value;record=self.inputs.get(alias)
            if record is None:continue
            assert record['repository_id']==identifier and record['target']==str(target),'Stale input repository identity'
            assert target.is_file() and target.stat().st_size==record['bytes'] and sha(target)==record['sha256'],'Client input changed after preparation'
            if changed is None:changed=list(command)
            changed[index+1]=str(target)
            self.event('client-input-argument',alias=str(alias),flag=command[index],argument_index=index+1,**record)
        return changed if changed is not None else command

    def history_request(self,source,destination,head_revision,ignore_uuid,minor_version):
        source=self.path(source);destination=self.path(destination)
        assert minor_version==5 and type(minor_version) is int,'Unsupported historical FSFS version'
        assert type(head_revision) is int and head_revision>=0 and type(ignore_uuid) is int and ignore_uuid==1
        assert source in self.repositories and destination in self.declared and destination not in self.repositories
        assert source!=destination and destination.parent==self.work/'svn-test-work/repositories'
        assert destination.resolve()==destination and not destination.exists()
        identifier=self.repository(source)
        return {'source_id':identifier,'source_alias':str(source),'destination_alias':str(destination),
                'head':head_revision,'minor_version':minor_version,'ignore_uuid':ignore_uuid}

    def copy_history(self,value,create,admin,refresh):
        assert value==self.history_request(value['source_alias'],value['destination_alias'],value['head'],value['ignore_uuid'],value['minor_version'])
        before=admin('repo','show','id:'+value['source_id'])
        assert before['id']==value['source_id'] and before['enabled'] and before['head']==value['head']
        assert str(uuid.UUID(before['uuid']))==before['uuid']
        tools_path=ROOT/'artifacts/pgvn/build/upstream-network-tools.json'
        tools=json.loads(tools_path.read_bytes());assert tools['build_manifest_sha256']==self.config['manifest_sha256']
        stock=ROOT/'runtime/pgvn/bin';native=ROOT/'runtime/pgvn/upstream-tools/bin'
        allowed={stock/name:self.manifest['runtime_dependencies']['bin/'+name] for name in ('svnadmin.exe','svnlook.exe')}
        for record in tools['products'].values():allowed[ROOT/record['path']]=record['sha256']
        def run(program,arguments,input=None):
            assert program in allowed and sha(program)==allowed[program],'Unfrozen historical fixture tool'
            self.event('historical-copy-tool',program=program.name)
            result=subprocess.run([str(program),*map(str,arguments)],cwd=self.work,env=dict(os.environ),input=input,
                capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=90)
            assert result.returncode==0,(program.name,result.stderr[-3000:])
            return result.stdout
        folder=(self.work/'global-fixture-inputs'/('historical-copy-'+uuid.uuid4().hex)).resolve()
        assert folder.is_relative_to(self.work/'global-fixture-inputs')
        folder.mkdir(parents=True,exist_ok=False);legacy=folder/'fsfs-1.5'
        source_dump=run(native/'pgvn-upstream-svnadmin.exe',['dump','--quiet',value['source_alias']])
        (folder/'source.dump').write_bytes(source_dump)
        run(stock/'svnadmin.exe',['create',legacy,'--fs-type=fsfs','--compatible-version=1.5'])
        run(stock/'svnadmin.exe',['load',legacy,'--quiet','--ignore-uuid'],input=source_dump)
        run(stock/'svnadmin.exe',['verify',legacy,'--quiet'])
        format_file=legacy/'db/format';assert format_file.read_text(encoding='ascii').splitlines()[0]=='3'
        expected_uuid=run(stock/'svnlook.exe',['uuid',legacy]).strip().decode('ascii')
        assert str(uuid.UUID(expected_uuid))==expected_uuid and expected_uuid!=before['uuid']
        assert int(run(stock/'svnlook.exe',['youngest',legacy]).strip())==value['head']
        legacy_dump=run(stock/'svnadmin.exe',['dump','--quiet',legacy]);dump_path=folder/'legacy.dump';dump_path.write_bytes(legacy_dump)
        source_after=admin('repo','show','id:'+value['source_id'])
        assert all(source_after[key]==before[key] for key in ('id','name','uuid','head','generation','enabled','routes')),'Source repository changed during preparation'
        create(value['destination_alias']);refresh()
        alias=self.path(value['destination_alias']);identifier=self.repository(alias)
        assert identifier!=value['source_id']
        run(native/'pgvn-upstream-svnadmin.exe',['load',alias,'--quiet','--force-uuid'],input=legacy_dump)
        after=admin('repo','show','id:'+identifier)
        assert after['id']==identifier and after['uuid']==expected_uuid and after['head']==value['head']
        verified=admin('verify','id:'+identifier);assert verified['verified_revisions']==value['head']+1
        assert not alias.exists() and not Path(value['source_alias']).exists()
        self.event('historical-copy-input',**value,destination_id=identifier,public_uuid=expected_uuid,
            source_dump=str(folder/'source.dump'),source_dump_sha256=sha(folder/'source.dump'),
            legacy_repository=str(legacy),fsfs_format_sha256=sha(format_file),fsfs_format=3,
            legacy_dump=str(dump_path),legacy_dump_sha256=sha(dump_path),verified_revisions=verified['verified_revisions'],
            method='Native source dump -> real stock FSFS compatible with SVN 1.5 -> stock dump -> native load --force-uuid; original log assertions unchanged.')
