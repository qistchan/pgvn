"""M4 component: unavailable database at startup keeps monitoring live."""
import datetime as dt
import hashlib
import json
import time
import traceback
import test_m3_credentials as setup
from test_m3_protocol_smoke import Node,client,PASSWORD
from test_m4_smoke import request,ready_status,until
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace("m3-credentials","m4-startup")


def checks():
    setup.setup_database()
    key=setup.WORK/"cold.key"; setup.admin("key","create",key,"cold-start-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repo=setup.admin("repo","create","cold","--route","/cold")
    rules=setup.WORK/"cold-authz.conf"; rules.write_text("[/]\nwriter = rw\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    token=setup.admin("monitor-token","create","--label","cold-start","--expires-in",120)["token"]
    node=Node("cold-node",key)
    setup.fixture.assert_ours()
    try:
        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS false",database="postgres")
        node.start()
        assert node.process.poll() is None
        request(node,"/health/live"); request(node,"/health/ready",expected=503)
        request(node,token=token,expected=401)  # No offline grant before any successful sample.
        started=time.monotonic()
        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS true",database="postgres")
        until(lambda: ready_status(node)==200,seconds=30)
        recovery=time.monotonic()-started
        overview,_=request(node,token=token)
        assert overview["ready"] and overview["freshness"]=="fresh"
        assert repo["uuid"].encode() in client("svn","info","--xml",node.url("/cold"))
        return {"live_during_startup_outage":True,"ready_503_until_validated":True,"no_cold_offline_auth":True,
            "automatic_business_start":True,"recovery_seconds":recovery,"real_svn_after_recovery":True}
    finally:
        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS true",database="postgres")
        node.stop()


def main():
    start=time.monotonic()
    try: details=checks(); status="PASS"; error=None
    except Exception: details={}; status="FAIL"; error=traceback.format_exc(); print(error,flush=True)
    report={"test_id":TEST_ID,"component":"M4 startup degradation","status":status,"milestone_complete":False,
        "created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"details":details,"error":error,
        "elapsed_seconds":time.monotonic()-start,"pgvnserve_sha256":hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest()}
    output=ROOT/"artifacts/pgvn/tests"; output.mkdir(parents=True,exist_ok=True); data=json.dumps(report,indent=2)+"\n"
    (output/(TEST_ID+".json")).write_text(data,encoding="utf-8"); (output/"m4-startup-latest.json").write_text(data,encoding="utf-8")
    print(json.dumps(report),flush=True); return 0 if status=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
