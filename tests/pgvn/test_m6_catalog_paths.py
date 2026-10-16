"""Run the M1 route contract in an already owned M6 network database.

This entry does not initialize/restart a cluster or create credentials. Only
the selected M1 function runs, with its SQL/DSN fixture bound to the named DB.
"""
from pathlib import Path
import argparse
import datetime as dt
import json
import re
import time
import traceback
import uuid
from fixture import Fixture,ROOT,RUNTIME
from test_m6_network_tools import sha,read,save

SELF='tests/pgvn/test_m6_catalog_paths.py'

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--database',required=True)
    database=parser.parse_args().database
    match=re.fullmatch('pgvn_m6_net_([0-9a-f]{12})',database);assert match
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';manifest=read(manifest_path);frozen=sha(manifest_path)
    for name in (SELF,'tests/pgvn/test_m1.py','tests/pgvn/core_probe.c'):
        assert sha(ROOT/name)==manifest['sources'][name],name
    probe=RUNTIME/'bin/core_probe.exe';assert sha(probe)==manifest['runtime_dependencies']['bin/core_probe.exe']
    for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256']
    plan_path=RUNTIME/'tests'/('m6-network-'+match[1])/'plan.json'
    plan=read(plan_path);assert plan['database']==database and plan['owner']=='pgvn_migrate_'+match[1]
    fixture=Fixture();fixture.assert_ours()
    assert fixture.sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='"+database+"'",database='postgres')==plan['owner']
    test_id='m6-catalog-paths-'+uuid.uuid4().hex[:12];work=RUNTIME/'tests'/test_id
    (work/'empty').mkdir(parents=True);start=time.monotonic();details=None;status='FAIL';error=None
    class ScopedFixture:
        def dsn(self,role='pgvn_service'):
            return f'host=127.0.0.1 port=16432 dbname={database} user={role} sslmode=disable'
        def sql(self,statement):return fixture.sql(statement,database=database)
    try:
        import test_m1 as original
        original.fixture=ScopedFixture();original.WORK=work;original.EMPTY=work/'empty'
        original.PROBE=probe
        details=original.creation_and_routing()
        assert details['literal_backslash_nodes_and_strict_routes'] is True
        assert sha(manifest_path)==frozen
        for name,record in manifest['products'].items():assert sha(ROOT/name)==record['sha256']
        assert sha(probe)==manifest['runtime_dependencies']['bin/core_probe.exe']
        assert list((work/'empty').iterdir())==[];status='PASS'
    except Exception:error=traceback.format_exc();print(error,flush=True)
    report={'test_id':test_id,'status':status,'component':'M6 scoped M1 route/path contract',
            'milestone_complete':False,'database':database,'work':str(work),
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,
            'build_manifest_sha256':frozen,'sources':{name:manifest['sources'][name] for name in (SELF,'tests/pgvn/test_m1.py','tests/pgvn/core_probe.c')},
            'core_probe_sha256':sha(probe),'upstream_python_tests_executed':0,
            'checks':['Original M1 creation_and_routing with added literal-node/strict-registration regression assertions'] if status=='PASS' else [],
            'elapsed_seconds':time.monotonic()-start,
            'scope':'Existing M6 owned database only; no cluster initialization, restart, other database writes, credential creation or network listeners.'}
    save(report,'m6-catalog-paths');return 0 if status=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
