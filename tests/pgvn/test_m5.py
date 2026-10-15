"""Frozen native M5 acceptance: complete prior gate and real DAV integration."""
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
import uuid
from fixture import ROOT,RUNTIME
from test_m4 import build_identity as prior_identity

TEST_ID='m5-'+uuid.uuid4().hex[:12]
RESULTS=ROOT/'artifacts/pgvn/tests';WORK=RUNTIME/'tests'/TEST_ID
SUITES=(('M0-M4 complete regressions','test_m4.py','m4'),
    ('native creation atomicity','test_m5_creation.py','m5-creation'),
    ('registry lifecycle and admin recovery','test_m5_registry.py','m5-registry'),
    ('actual M4 schema upgrade','test_m5_upgrade.py','m5-upgrade'),
    ('HTTP v1/v2 and Unicode author','test_m5_http.py','m5-http'),
    ('HTTP/HTTPS round robin and SVNKit','test_m5_balancer.py','m5-balancer'),
    ('path and historical authorization','test_m5_authz.py','m5-authz'),
    ('upstream Unicode client limitation baseline','test_m5_unicode_baseline.py','m5-unicode-baseline'),
    ('independent Apache telemetry ledger','test_m5_telemetry.py','m5-telemetry'),
    ('LocalService worker lifecycle','test_m5_service.py','m5-dav-service'))
DAV_BROWSER_CHECKS=('worker_details','http_method_counts','request_error_id','refresh_retains_open_and_focus',
    'lost_worker_and_new_instance','narrow_details_layout')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def identity():
    manifest=prior_identity()
    assert manifest['milestone']=='M5' and manifest['capabilities']['dav_protocol'] is True
    assert manifest['dav_patches']['provider']=='pgvn'
    assert 'dav_pgvn_module' in manifest['dav_patches']['public_exports']
    for name,metadata in manifest['products'].items():
        if '/apache/modules/' in name:
            assert not re.search(r'Name: (?:dav_svn|authz_svn)\w*\b',metadata['pe']),name
    assert manifest['apache_dependencies']
    for name,expected in manifest['apache_dependencies'].items():assert digest(RUNTIME/name)==expected,name
    return manifest


def browser(manifest):
    report=json.loads((RESULTS/'m5-browser-latest.json').read_text(encoding='utf-8'))
    assert report['status']=='PASS' and report['fixture_stopped'] is True
    assert report['pgvnserve_sha256']==manifest['products']['runtime/pgvn/bin/pgvnserve.exe']['sha256']
    assert report['dav_host_sha256']==manifest['products']['runtime/pgvn/apache/bin/pgvnhttpd.exe']['sha256']
    assert report['dav_host_runtime']['process_code_page']==65001
    for name,expected in report['modules'].items():assert digest(RUNTIME/'apache/modules'/name)==expected,name
    for name,expected in report['assets'].items():assert digest(ROOT/'web/pgvn-monitor'/name)==expected,name
    assert set(report['assets'])=={'index.html','monitor.js','monitor.css'}
    assert all(report['checks'].get(name) is True for name in DAV_BROWSER_CHECKS)
    return report


def main():
    WORK.mkdir(parents=True);records=[];manifest={};ui={};start=time.monotonic()
    try:
        manifest=identity();ui=browser(manifest)
        for name,script,key in SUITES:
            before=time.monotonic()
            with (WORK/(key+'.log')).open('wb') as log:
                process=subprocess.run([sys.executable,str(ROOT/'tests/pgvn'/script)],cwd=ROOT,
                    stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,timeout=1800 if key=='m4' else 360)
            assert process.returncode==0,name+' failed; see '+str(WORK/(key+'.log'))
            path=RESULTS/(key+'-latest.json');report=json.loads(path.read_text(encoding='utf-8'));assert report['status']=='PASS',name
            for module,expected in report.get('modules',{}).items():assert digest(RUNTIME/'apache/modules'/module)==expected,module
            for field,product in (('pgvnserve_sha256','bin/pgvnserve.exe'),('pgvnadmin_sha256','bin/pgvnadmin.exe'),
                ('dav_state_probe_sha256','bin/dav_state_probe.exe'),('dav_registry_probe_sha256','bin/dav_registry_probe.exe')):
                if field in report:assert report[field]==digest(RUNTIME/product),field
            record={'name':name,'status':'PASS','test_id':report['test_id'],'elapsed_seconds':time.monotonic()-before,
                'report_sha256':digest(path),'report':report};records.append(record)
            print(json.dumps({key:record[key] for key in ('name','status','test_id','elapsed_seconds')}),flush=True)
        assert identity()==manifest and browser(manifest)==ui,'Frozen acceptance inputs changed'
        status='PASS';error=None
    except Exception:status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'milestone':'M5','status':status,'milestone_complete':status=='PASS','records':records,
        'browser':ui,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-start,
        'build_manifest_sha256':digest(ROOT/'artifacts/pgvn/build/manifest.json'),
        'products':{name:metadata['sha256'] for name,metadata in manifest.get('products',{}).items()},'error':error,
        'scope':'Pinned native Windows Subversion 1.14.5 / PostgreSQL / Apache and SVNKit 1.10.1. Local independent processes, HTTP/HTTPS certificate checks, persistent DAV state, authz, worker monitoring. Known SVNKit Unicode revprop limitation reproduced against stock FSFS. Cross-host failure domains/performance are M6; clean-machine/Polarion application acceptance is M7.'}
    RESULTS.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (RESULTS/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(RESULTS/'m5-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({'test_id':TEST_ID,'status':status,'groups':len(records),'milestone_complete':report['milestone_complete']}),flush=True)
    return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
