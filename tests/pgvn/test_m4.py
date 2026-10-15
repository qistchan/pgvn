"""Frozen M4 release acceptance, including M0-M3 and real browser evidence."""
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
from test_m3 import build_identity as prior_identity

TEST_ID="m4-"+uuid.uuid4().hex[:12]
WORK=RUNTIME/"tests"/TEST_ID
RESULTS=ROOT/"artifacts/pgvn/tests"
SUITES=(
    ("M0-M3 regressions","test_m3.py","m3"),
    ("authentication, snapshots and recovery","test_m4_smoke.py","m4-smoke"),
    ("API and independent wire accounting","test_m4_api.py","m4-api"),
    ("cold PostgreSQL startup and recovery","test_m4_startup.py","m4-startup"),
    ("LocalService TCP blackhole and slow HTTP shutdown","test_m4_network.py","m4-network"),
    ("publication, receipts and writer lifecycles","test_m4_ledger.py","m4-ledger"),
    ("lost PostgreSQL COMMIT acknowledgement","test_m4_receipt_fault.py","m4-receipt-fault"),
    ("capacity, isolation and measured overhead","test_m4_resources.py","m4-resources"),
)
BROWSER_CHECKS=("all_views","repository_pagination","repository_search","repository_detail",
    "text_only_hostile_name","keyboard_focus_and_refresh","configurable_refresh","narrow_layout",
    "paused_refresh","stale_database_and_unknown_nodes","lost_peer_and_recovery",
    "token_revocation_clears_data","reload_requires_token","no_persistent_token")


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def build_identity():
    manifest=prior_identity()
    assert manifest["milestone"]=="M4" and manifest["capabilities"]["monitor"]=="full"
    actual={}
    for root,suffixes in (("web",None),("src",None),("sql",None),
        ("scripts/pgvn",{".py",".ps1"}),("tests/pgvn",{".py",".c",".inc",".java"})):
        for path in sorted((ROOT/root).rglob("*")):
            if path.is_file() and (suffixes is None or path.suffix in suffixes): actual[path.relative_to(ROOT).as_posix()]=digest(path)
    assert actual==manifest["sources"],"Source inventory changed after the frozen Release build"
    for path,expected in manifest["runtime_dependencies"].items(): assert digest(RUNTIME/path)==expected,path
    return manifest


def browser_evidence(manifest):
    report=json.loads((RESULTS/"m4-browser-latest.json").read_text(encoding="utf-8"))
    assert report["status"]=="PASS" and report["fixture_stopped"] is True
    assert report["pgvnserve_sha256"]==manifest["products"]["runtime/pgvn/bin/pgvnserve.exe"]["sha256"]
    for name,expected in report["assets"].items(): assert digest(ROOT/"web/pgvn-monitor"/name)==expected,name
    assert set(report["assets"])=={"index.html","monitor.js","monitor.css"}
    assert all(report["checks"].get(check) is True for check in BROWSER_CHECKS),report["checks"]
    return report


def main():
    WORK.mkdir(parents=True); records=[]; manifest={}; browser={}; start=time.monotonic()
    try:
        manifest=build_identity(); browser=browser_evidence(manifest)
        for name,script,key in SUITES:
            before=time.monotonic()
            with (WORK/(key+".log")).open("wb") as log:
                process=subprocess.run([sys.executable,str(Path(__file__).with_name(script))],cwd=ROOT,
                    stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,timeout=900 if key=="m3" else 360)
            assert process.returncode==0,f"{name} failed; see {WORK/(key+'.log')}"
            report=json.loads((RESULTS/(key+"-latest.json")).read_text(encoding="utf-8"))
            assert report["status"]=="PASS",name
            for field,path in (("pgvnserve_sha256","bin/pgvnserve.exe"),("core_probe_sha256","bin/core_probe.exe")):
                if field in report: assert report[field]==digest(RUNTIME/path),field
            record={"name":name,"status":"PASS","test_id":report["test_id"],"elapsed_seconds":time.monotonic()-before,
                "report_sha256":digest(RESULTS/(key+"-latest.json")),"report":report}
            records.append(record); print(json.dumps({key:record[key] for key in ("name","status","test_id","elapsed_seconds")}),flush=True)
        assert build_identity()==manifest,"Build changed during acceptance"
        assert browser_evidence(manifest)==browser,"Browser evidence changed during acceptance"
        status="PASS"; error=None
    except Exception:
        status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"milestone":"M4","status":status,"milestone_complete":status=="PASS",
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"elapsed_seconds":time.monotonic()-start,
        "work_directory":str(WORK),"records":records,"browser":browser,"error":error,
        "build_manifest_sha256":digest(ROOT/"artifacts/pgvn/build/manifest.json"),
        "products":{name:metadata["sha256"] for name,metadata in manifest.get("products",{}).items()},
        "scope":"Native Windows read-only embedded monitor with SVN telemetry; Apache aggregation is M5, formal mixed-load performance budgets are M6."}
    RESULTS.mkdir(parents=True,exist_ok=True); data=json.dumps(report,indent=2)+"\n"
    (RESULTS/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (RESULTS/"m4-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps({"test_id":TEST_ID,"status":status,"groups":len(records),"milestone_complete":report["milestone_complete"]}),flush=True)
    return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
