"""Explicit file/configuration boundaries for original network authz tests.

Valid configurations use the production administrator. Invalid-file tests
append a malformed authz version as their non-superuser fixture owner, so the
real protocol reads and rejects that data. No protocol response is simulated.
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

def namespace(text,alias,canonical):
    """Translate only the exact fixture repository name in section headers."""
    original=Path(alias).name
    pattern=re.compile(r'(?m)^(\s*\[)'+re.escape(original)+r'(:[^\]\r\n]*\])')
    changed,count=pattern.subn(lambda match:match[1]+canonical+match[2],text)
    return changed,{'original':original,'canonical':canonical,'translated_sections':count}

def authz_rejection(result):
    if result.returncode!=2:return False
    try:error=json.loads(result.stderr)['error']
    except (ValueError,KeyError,TypeError):return False
    return isinstance(error,dict) and error.get('code')=='invalid_arguments' and error.get('message') in (
        'Invalid authorization rules','Invalid authz configuration')

def fixture_sql(config,manifest,work,current):
    """Guard the exact owned network database before returning an SQL runner."""
    work=Path(work).resolve();database=config['database']
    match=re.fullmatch('pgvn_m6_net_([0-9a-f]{12})',database);assert match
    suffix=match[1];owner='pgvn_migrate_'+suffix
    expected_work=(ROOT/'runtime/pgvn/tests'/('m6-network-'+suffix)/'pgvn').resolve()
    assert config['backend']=='pgvn' and work==expected_work and work.is_dir()
    plan_path=work.parent/'plan.json';plan=json.loads(plan_path.read_bytes())
    assert plan['database']==database and plan['owner']==owner
    assert plan['endpoint']=='127.0.0.1:16432' and (work.parent/'started.json').is_file()
    assert sha(ROOT/'artifacts/pgvn/build/manifest.json')==plan['inputs']['manifest_sha256']
    identifier=str(uuid.UUID(current['id']));generation=current['generation']
    assert identifier==current['id'] and type(generation) is int and 1<=generation<2**63-1
    environment={name:value for name,value in os.environ.items() if not name.upper().startswith('PG')}
    environment.update(PGCONNECT_TIMEOUT='5',PGSSLMODE='disable',PGCLIENTENCODING='UTF8')
    psql=Path(manifest['inputs']['postgres'])/'bin/psql.exe'
    def sql(role,statement):
        result=subprocess.run([str(psql),'-X','-w','-qAt','-h','127.0.0.1','-p','16432','-U',role,
            '-d',database,'--set=ON_ERROR_STOP=1','-f','-'],input=statement,env=environment,
            capture_output=True,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        assert result.returncode==0,result.stderr
        return result.stdout.strip()
    # The cluster-owner connection is read-only. Mutation uses the exact
    # non-superuser database owner, never the installed cluster or pgfs_poc.
    check=json.loads(sql('pgvn_owner',"SELECT json_build_object('directory',current_setting('data_directory'),"
        "'database',current_database(),'owner',pg_get_userbyid(datdba)) FROM pg_database WHERE datname=current_database();"))
    assert Path(check['directory']).resolve()==(ROOT/'runtime/pgvn/pg-primary').resolve()
    assert check['database']==database and check['owner']==owner
    guard=f"""DO $identity$ BEGIN
 IF current_database()<>'{database}' OR current_user<>'{owner}' OR
    EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolcreatedb OR rolcreaterole)) THEN
  RAISE EXCEPTION 'Invalid isolated fixture identity';
 END IF;
 PERFORM 1 FROM pgvn.repositories WHERE repository_id='{identifier}' AND generation={generation} FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Fixture configuration generation changed'; END IF;
END $identity$;
"""
    def mutate(statement,query):return sql(owner,'BEGIN;\n'+guard+statement+'\nCOMMIT;\n'+query)
    return mutate,identifier,generation,owner


def text(value):
    return 'NULL' if value is None else "convert_from(decode('"+value.encode('utf-8').hex()+"','hex'),'UTF8')"


def route_alias(config,manifest,work,current,alias):
    """Mirror the stock Apache DDT parent mount using a second catalog route."""
    path=Path(alias).resolve();parent=Path(work).resolve()/'svn-test-work/repositories'
    assert path.parent==parent and re.fullmatch(r'[A-Za-z0-9_.-]+',path.name)
    assert not path.exists(),'Native alias must remain absent'
    mutate,identifier,generation,owner=fixture_sql(config,manifest,work,current)
    original='/svn-test-work/repositories/'+path.name
    route='/ddt-test-work/repositories/'+path.name
    statement=f"""DO $route$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pgvn.routes WHERE route_prefix={text(original)} AND repository_id='{identifier}') THEN
  RAISE EXCEPTION 'DDT fixture has no matching original route';
 END IF;
 INSERT INTO pgvn.routes(route_prefix,repository_id) VALUES({text(route)},'{identifier}') ON CONFLICT DO NOTHING;
 IF NOT EXISTS(SELECT 1 FROM pgvn.routes WHERE route_prefix={text(route)} AND repository_id='{identifier}') THEN
  RAISE EXCEPTION 'DDT fixture route belongs to another repository';
 END IF;
END $route$;
"""
    assert mutate(statement,f"SELECT repository_id::text FROM pgvn.routes WHERE route_prefix={text(route)};")==identifier
    return {'repository_id':identifier,'route':route,'database':config['database'],'role':owner}


def invalid_file(config,manifest,work,current,authz,groups,hooks,settings):
    """Inject only an admin-rejected invalid authz payload in an owned fixture."""
    assert isinstance(authz,str) and len(authz.encode('utf-8'))<=1048576
    assert groups is None or isinstance(groups,str) and len(groups.encode('utf-8'))<=1048576
    mutate,identifier,generation,owner=fixture_sql(config,manifest,work,current)
    values=(text(authz),text(groups),text(json.dumps(hooks)),text(json.dumps(settings)))
    statement=f"""
DO $fixture$ BEGIN
 IF NOT pgvn.valid_hooks({values[2]}::jsonb) OR NOT pgvn.valid_settings({values[3]}::jsonb) THEN
  RAISE EXCEPTION 'Invalid non-authz fixture data';
 END IF;
 PERFORM 1 FROM pgvn.repositories WHERE repository_id='{identifier}' FOR UPDATE;
 INSERT INTO pgvn.configurations(repository_id,generation,authz_text,groups_text,hooks,settings,published_by)
 VALUES('{identifier}',{generation+1},{values[0]},{values[1]},{values[2]}::jsonb,{values[3]}::jsonb,'upstream-invalid-file-fixture');
 UPDATE pgvn.repositories SET generation=generation+1 WHERE repository_id='{identifier}' AND generation={generation};
 IF NOT FOUND THEN RAISE EXCEPTION 'Fixture publication lost generation'; END IF;
END $fixture$;
"""
    assert mutate(statement,f"SELECT generation FROM pgvn.repositories WHERE repository_id='{identifier}';")==str(generation+1)
    return {'repository_id':identifier,'generation':generation+1,'database':config['database'],'role':owner,
            'authz_sha256':hashlib.sha256(authz.encode('utf-8')).hexdigest(),
            'method':'Admin rejected malformed authz; owned raw-file fixture appended it for the real server to parse.'}
