"""One revision, repeated commit receipt and explicit lookup must stay distinct."""
import datetime as dt
import hashlib
import json
import time
import traceback
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-ledger")


def checks():
    setup.setup_database()
    repo=setup.admin("repo","create","ledger","--route","/ledger")
    rules=setup.WORK/"ledger-authz.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    writer=json.loads(setup.run(setup.PROBE,["writer-ledger",repo["id"],"writer"],role="pgvn_service").stdout)
    assert [writer[k] for k in ("writers_before","writers_open","writers_closed","writers_aborted")]==[0,1,0,0],writer
    # A writer pins the already leased FS session; it must not acquire another
    # connection or return the FS connection to a pool when the stream closes.
    assert writer["connections_open"]==writer["connections_before"] and writer["connections_closed"]==writer["connections_before"],writer
    assert writer["sql_active"]==0
    result=setup.run(setup.PROBE,["publication-ledger",repo["id"],"writer"],role="pgvn_service")
    actual=json.loads(result.stdout)
    assert {k:actual[k] for k in ("first","repeat","receipt","head","published_delta","receipt_delta")}=={"first":1,"repeat":1,"receipt":1,"head":1,"published_delta":1,"receipt_delta":2},actual
    assert actual["sql_calls_delta"]==actual["sql_results_delta"]
    assert actual["sql_active"]==0 and actual["writers_active"]==0 and actual["sql_errors_delta"]==0
    assert setup.sql("SELECT count(*) FROM pgfs.revisions WHERE rev>0")=="1"
    assert setup.sql("SELECT count(*) FROM pgfs.transactions WHERE state='active'")=="0"
    setup.admin("verify",repo["id"])
    return {"ledger":actual,"writer_lifecycle":writer,"exactly_one_persisted_revision":True,"full_verify":True,
        "limits":"Lost database COMMIT reply is checked by the separate transport fault component."}


def main():
    start=time.monotonic()
    try: details=checks(); status="PASS"; error=None
    except Exception: details={}; status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 publication ledger","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,
        "elapsed_seconds":time.monotonic()-start,"core_probe_sha256":hashlib.sha256(setup.PROBE.read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests"; output.mkdir(parents=True,exist_ok=True); data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (output/"m4-ledger-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True); return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
