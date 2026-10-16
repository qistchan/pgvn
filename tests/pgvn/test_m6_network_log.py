"""Offline guards for log-suite client inputs and historical copy setup."""
from pathlib import Path
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';frozen=sha(manifest_path)
    manifest=json.loads(manifest_path.read_bytes());source=ROOT/'tests/pgvn/upstream_network_log.py'
    for name in ('tests/pgvn/upstream_network_log.py','tests/pgvn/test_m6_network_log.py'):
        assert sha(ROOT/name)==manifest['sources'][name],'Unregistered log fixture input'
    spec=importlib.util.spec_from_file_location('pgvn_log_offline',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    test_id='m6-network-log-offline-'+uuid.uuid4().hex[:12]
    work=ROOT/'runtime/pgvn/tests'/test_id;work.mkdir(parents=True,exist_ok=False)
    fake=work/'fake-root';module.ROOT=fake
    area=fake/'runtime/pgvn/tests/m6-network-123456abcdef/pgvn';area.mkdir(parents=True)
    first=area/'svn-test-work/repositories/first';second=first.with_name('second');third=first.with_name('historical')
    first_id='11111111-1111-1111-1111-111111111111';second_id='22222222-2222-2222-2222-222222222222'
    registry={first:first_id,second:second_id};declared={first,second,third}
    fake_manifest=fake/'artifacts/pgvn/build/manifest.json';fake_manifest.parent.mkdir(parents=True)
    fake_manifest.write_text(json.dumps({'sources':{'tests/pgvn/upstream_network_log.py':sha(source)}}),encoding='utf-8')
    config={'database':'pgvn_m6_net_123456abcdef','backend':'pgvn','suite':'log_tests','cases':[1,37],
            'work':str(area),'identity_domain':'upstream-123456abcdef','manifest_sha256':sha(fake_manifest)}
    plan={'test_id':'m6-network-123456abcdef','work':area.parent.relative_to(fake).as_posix(),
          'suite':'log_tests','cases':config['cases'],'database':config['database'],'owner':'pgvn_migrate_123456abcdef',
          'endpoint':'127.0.0.1:16432','cluster_data_directory':str(fake/'runtime/pgvn/pg-primary'),
          'inputs':{'manifest_sha256':sha(fake_manifest)}}
    plan_path=area.parent/'plan.json';started=area.parent/'started.json'
    def seal():
        plan_path.write_text(json.dumps(plan),encoding='utf-8')
        started.write_text(json.dumps({'plan_sha256':sha(plan_path)}),encoding='utf-8')
    seal();events=[];checks=[];status='FAIL';error=None
    def construct(**changes):
        values=dict(config=config,work=area,repositories=registry,declared=declared,
                    event=lambda event,**fields:events.append(dict(event=event,**fields)))
        values.update(changes);return module.LogFixtures(**values)
    def reject(operation,exception=(AssertionError,ValueError,FileNotFoundError)):
        try:operation()
        except exception:return
        raise AssertionError('Expected log fixture boundary rejection')
    def check(name,operation):operation();checks.append({'name':name,'status':'PASS'})
    calls=[];sentinel=object()
    def writer(path,contents,mode='w'):
        calls.append((path,contents,mode))
        if 'b' in mode:
            with open(path,mode) as output:output.write(contents)
        else:
            with open(path,mode,encoding='utf-8',newline='') as output:output.write(contents)
        return sentinel
    try:
        fixture=construct()
        check('Exact started plan binds the isolated database, owner, cluster and frozen source',lambda:None)
        check('Wrong database prefix rejected',lambda:reject(lambda:construct(config=dict(config,database='pgfs_poc'))))
        check('Another upstream suite rejected',lambda:reject(lambda:construct(config=dict(config,suite='diff_tests'))))
        check('Wrong fixture root rejected',lambda:reject(lambda:construct(work=work)))
        plan['owner']='other_owner';seal()
        check('A consistently resealed but wrong owner is rejected',lambda:reject(construct))
        plan['owner']='pgvn_migrate_123456abcdef';plan['endpoint']='127.0.0.1:5432';seal()
        check('Installed-cluster endpoint rejected',lambda:reject(construct))
        plan['endpoint']='127.0.0.1:16432';plan['cluster_data_directory']=str(fake/'other-cluster');seal()
        check('Different cluster data directory rejected',lambda:reject(construct))
        plan['cluster_data_directory']=str(fake/'runtime/pgvn/pg-primary');seal()
        plan_path.write_text('{}',encoding='utf-8')
        check('Plan changed after the start marker is rejected',lambda:reject(construct));seal()
        check('Stale frozen build rejected',lambda:reject(lambda:construct(config=dict(config,manifest_sha256='bad'))))
        path=first/'log-msg';payload='提交说明\r\nsecond line\n'
        assert fixture.write(writer,str(path),payload,mode='w') is sentinel
        target=area/'global-client-input'/first_id/'log-msg'
        assert target.read_bytes()==payload.encode('utf-8') and calls[-1][1] is payload and calls[-1][2]=='w'
        assert not first.exists()
        check('Text, CRLF, positional path and keyword mode pass unchanged to the original writer',lambda:None)
        payload=b'\x00binary\r\n\xff'
        assert fixture.write(writer,str(path),payload,'wb') is sentinel
        assert target.read_bytes()==payload and calls[-1][1] is payload and calls[-1][2]=='wb'
        check('Binary input and positional mode remain byte-identical',lambda:None)
        binary=str(fake/'runtime/pgvn/bin/svn.exe')
        for flag in ('-F','--file'):
            command=[binary,'ci',flag,str(path),'working-copy','--username','jrandom']
            mapped=fixture.command(command,binary)
            assert mapped==[binary,'ci',flag,str(target),'working-copy','--username','jrandom']
            assert command[3]==str(path)
        check('Only the recorded SVN file argument changes; caller sequence and all other arguments remain intact',lambda:None)
        command=[str(fake/'runtime/pgvn/bin/svnmucc.exe'),'ci','-F',str(path)]
        assert fixture.command(command,binary) is command
        check('Another executable is never redirected',lambda:None)
        command=[binary,'ci','-F',str(second/'log-msg')]
        assert fixture.command(command,binary) is command and not second.exists()
        check('An unwritten input retains the original missing-file behavior',lambda:None)
        ordinary=area/'ordinary-log-msg';assert fixture.write(writer,str(ordinary),b'ordinary','wb') is sentinel
        assert calls[-1][0]==str(ordinary) and ordinary.read_bytes()==b'ordinary'
        check('Ordinary filesystem writes retain their original path',lambda:None)
        check('Unknown repository is not redirected',lambda:reject(lambda:fixture.write(writer,first.with_name('unknown')/'log-msg','x')))
        check('Other filenames under a native alias remain real missing-directory failures',lambda:reject(lambda:fixture.write(writer,first/'format','x')))
        registry[first]='invalid'
        check('Malformed registered repository UUID rejected',lambda:reject(lambda:fixture.input(path)))
        registry[first]=first_id;first.mkdir(parents=True)
        check('A materialized native alias is rejected rather than hidden',lambda:reject(lambda:fixture.input(path)))
        first.rmdir();registry[first]=second_id
        command=[binary,'ci','-F',str(path)]
        check('Input cannot be reused after repository identity changes',lambda:reject(lambda:fixture.command(command,binary)))
        registry[first]=first_id;target.write_bytes(b'tampered')
        check('Input changed after preparation is rejected before the client runs',lambda:reject(lambda:fixture.command(command,binary)))
        fixture.write(writer,path,payload,'wb')
        request=lambda **changes:fixture.history_request(**dict(dict(source=first,destination=third,head_revision=1,
            ignore_uuid=1,minor_version=5),**changes))
        value=request();assert value['source_id']==first_id and value['head']==1 and value['minor_version']==5
        check('Historical copy request names a registered source and a declared empty target',lambda:None)
        check('Unrequested old format rejected',lambda:reject(lambda:request(minor_version=6)))
        check('Unsupported UUID mode rejected',lambda:reject(lambda:request(ignore_uuid=0)))
        check('Invalid revision count rejected',lambda:reject(lambda:request(head_revision=-1)))
        check('Undeclared historical copy target rejected',lambda:reject(lambda:request(destination=first.with_name('unknown'))))
        outside=area/'outside';declared.add(outside)
        check('Historical target outside the repository mount rejected',lambda:reject(lambda:request(destination=outside)))
        third.mkdir(parents=True)
        check('Materialized historical target rejected',lambda:reject(request));third.rmdir()
        outside_source=fake/'outside-source';registry[outside_source]=second_id
        check('Registered historical source outside the fixture root rejected',lambda:reject(lambda:request(source=outside_source)))
        del registry[outside_source]
        assert sum(item['event']=='client-input-write' for item in events)==3
        assert sum(item['event']=='client-input-argument' for item in events)==2
        assert all(item.get('sha256') and item.get('repository_id')==first_id for item in events)
        check('Every redirected write and client argument records its identity and byte hash',lambda:None)
        assert sha(manifest_path)==frozen and not first.exists() and not second.exists() and not third.exists()
        status='PASS'
    except BaseException:error=traceback.format_exc();print(error,file=sys.stderr)
    finally:module.ROOT=ROOT
    report={'test_id':test_id,'status':status,'component':'M6 log fixture offline guards',
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':checks,'error':error,
            'build_manifest_sha256':frozen,'real_subprocesses_started':0,'database_connections_attempted':0,
            'upstream_python_tests_executed':0,'milestone_complete':False}
    path=ROOT/'artifacts/pgvn/tests'/(test_id+'.json');path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'test_id':test_id,'status':status,'checks':len(checks)}))
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
