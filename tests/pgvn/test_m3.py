"""M3 release gate: exact build identity, prior regressions, real protocol/service checks."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
import traceback
import uuid
from fixture import ROOT,RUNTIME

TEST_ID="m3-"+uuid.uuid4().hex[:12]
WORK=RUNTIME/"tests"/TEST_ID
RESULTS=ROOT/"artifacts/pgvn/tests"
SUITES=(
    ("M0 regression","test_m0.py","m0"),
    ("M1 regression","test_m1.py","m1"),
    ("M2 regression","test_m2.py","m2"),
    ("encrypted credentials and rotation","test_m3_credentials.py","m3-credentials"),
    ("SVN and SVNKit protocol","test_m3_protocol_smoke.py","m3-protocol-smoke"),
    ("external hooks and lost reply","test_m3_hooks.py","m3-hooks"),
    ("LocalService business drain","test_m3_service.py","m3-service"),
)


def hash_file(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def build_identity():
    manifest=json.loads((ROOT/"artifacts/pgvn/build/manifest.json").read_text())
    assert manifest["milestone"] in ("M3","M4-dev","M4","M5-dev","M5") and manifest["configuration"]=="Release"
    assert manifest["capabilities"]["svn_protocol"] is True
    for path,expected in manifest["sources"].items(): assert hash_file(ROOT/path)==expected,path
    for path,metadata in manifest["products"].items(): assert hash_file(ROOT/path)==metadata["sha256"],path
    return manifest


def main():
    WORK.mkdir(parents=True); records=[]; manifest={}; start=time.monotonic()
    try:
        manifest=build_identity()
        for name,script,key in SUITES:
            before=time.monotonic()
            with (WORK/(key+".log")).open("wb") as log:
                process=subprocess.run([sys.executable,str(Path(__file__).with_name(script))],cwd=ROOT,
                    stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,timeout=360)
            assert process.returncode==0,f"{name} failed; see {WORK/(key+'.log')}"
            report=json.loads((RESULTS/(key+"-latest.json")).read_text())
            assert report["status"]=="PASS",name
            if "pgvnserve_sha256" in report:
                assert report["pgvnserve_sha256"]==manifest["products"]["runtime/pgvn/bin/pgvnserve.exe"]["sha256"]
            if "pgvnadmin_sha256" in report:
                assert report["pgvnadmin_sha256"]==manifest["products"]["runtime/pgvn/bin/pgvnadmin.exe"]["sha256"]
            record={"name":name,"status":"PASS","test_id":report["test_id"],"elapsed_seconds":time.monotonic()-before,
                "report_sha256":hash_file(RESULTS/(key+"-latest.json")),"report":report}
            records.append(record)
            print(json.dumps({field:record[field] for field in ("name","status","test_id","elapsed_seconds")}),flush=True)
        assert build_identity()==manifest,"Build changed during acceptance"
        status="PASS"; error=None
    except Exception:
        status="FAIL"; error=traceback.format_exc()
        print(error,flush=True)
    report={"test_id":TEST_ID,"milestone":"M3","status":status,"milestone_complete":status=="PASS",
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"elapsed_seconds":time.monotonic()-start,
        "work_directory":str(WORK),"records":records,"error":error,
        "build_manifest_sha256":hash_file(ROOT/"artifacts/pgvn/build/manifest.json"),
        "products":{name:metadata["sha256"] for name,metadata in manifest.get("products",{}).items()},
        "scope":"Pinned native Windows SVN 1.14.5 / SVNKit 1.10.1 protocol and two-process shared PostgreSQL; DAV, full monitor UI and Polarion application acceptance are M4-M7."}
    RESULTS.mkdir(parents=True,exist_ok=True)
    data=json.dumps(report,indent=2)+"\n"
    (RESULTS/(TEST_ID+".json")).write_text(data,encoding="utf-8")
    (RESULTS/"m3-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps({"test_id":TEST_ID,"status":status,"groups":len(records),"milestone_complete":report["milestone_complete"]}),flush=True)
    return 0 if status=="PASS" else 1


if __name__=="__main__": sys.exit(main())
