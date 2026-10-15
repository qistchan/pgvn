"""Drop the confirmed PG COMMIT response and compare durable history/counters."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import traceback
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME
from tcp_proxy import TcpProxy

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-receipt-fault")


def checks():
    setup.setup_database();setup.fixture.assert_ours()
    repo=setup.admin("repo","create","reply-fault","--route","/reply-fault")
    rules=setup.WORK/"rules.conf";rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    proxy=TcpProxy(16432,drop_publication_reply=True)
    try:
        dsn=setup.dsn("pgvn_service").replace("port=16432",f"port={proxy.port}")
        result=subprocess.run([str(setup.PROBE),"publication-ledger",repo["id"],"writer"],cwd=setup.EMPTY,
            env=dict(os.environ,PGVN_DSN=dsn),capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0,(result.stdout,result.stderr)
        actual=json.loads(result.stdout)
        assert proxy.dropped.is_set(),"No publication COMMIT reply was dropped"
        assert {k:actual[k] for k in ("first","repeat","receipt","head","published_delta","receipt_delta")}=={
            "first":1,"repeat":1,"receipt":1,"head":1,"published_delta":1,"receipt_delta":3},actual
        assert actual["sql_active"]==0 and actual["writers_active"]==0
        assert actual["sql_errors_delta"]>=1 and actual["sql_class_08_delta"]>=1
        assert actual["sql_calls_delta"]==actual["sql_results_delta"]
        assert setup.sql("SELECT count(*) FROM pgfs.revisions WHERE rev>0")=="1"
        assert setup.sql("SELECT count(*) FROM pgfs.transactions WHERE state='active'")=="0"
        setup.admin("verify",repo["id"])
        return {"server_commit_ack_observed_then_client_reply_dropped":True,"ledger":actual,
            "exactly_one_persisted_revision":True,"full_verify":True}
    finally:proxy.close()


def main():
    start=time.monotonic()
    try:details=checks();status="PASS";error=None
    except Exception:details={};status="FAIL";error=traceback.format_exc();print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 lost PostgreSQL COMMIT reply","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,"elapsed_seconds":time.monotonic()-start,
        "core_probe_sha256":hashlib.sha256(setup.PROBE.read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests";output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8");(output/"m4-receipt-fault-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True);return 0 if status=="PASS" else 1


if __name__=="__main__":raise SystemExit(main())
