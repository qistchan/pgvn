"""Disposable real-service browser fixture; commands affect only its own DB/nodes.

Prints a newly generated monitor token once. It is not written to the session
file. Write {"sequence":1,"action":"stop-peer"} to the reported control file.
Actions: stop-peer, start-peer, block-db, restore-db, revoke-token, stop.
The fixture expires after fifteen minutes and always restores DB access.
Use --dav --dav-peer for two simultaneously live DAV instances.
"""
import hashlib
import argparse
import json
import time
import test_m3_credentials as setup
from fixture import ROOT, RUNTIME
from test_m3_protocol_smoke import Node, PASSWORD, client
from test_m4_smoke import ready_status, until


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--dav',action='store_true')
    parser.add_argument('--dav-peer',action='store_true');args=parser.parse_args()
    if args.dav_peer and not args.dav:parser.error('--dav-peer requires --dav')
    setup.setup_database(); setup.fixture.assert_ours()
    key=setup.WORK/"browser.key"; setup.admin("key","create",key,"browser-v1")
    setup.admin("user","set","pg vn test","writer","--key-file",key,input=PASSWORD)
    repo=setup.admin("repo","create","工程文档","--route","/ui-demo")
    rules=setup.WORK/"rules.conf"
    rules.write_text("[/]\nwriter = rw\n[工程文档:/forbidden]\nwriter = r\n",encoding="utf-8")
    setup.admin("config","publish",repo["id"],"--expected-generation",1,"--authz",rules)
    setup.admin("repo","create","<img src=x onerror=window.pgvnXss=true>","--route","/hostile")
    for i in range(51): setup.admin("repo","create",f"分页仓库 {i:02}","--route",f"/page-{i}")
    issued=setup.admin("monitor-token","create","--label","disposable browser fixture","--expires-in",1800)
    a,b=Node("browser-main",key),Node("browser-peer",key)
    dav=None;dav_peer=None
    if args.dav:
        import test_m5_http as dav_test
        dav=dav_test.Apache('browser-dav')
        dav.config.write_text(dav.config.read_text(encoding='utf-8').replace('<Location /svn>','<Location />'),encoding='utf-8')
        if args.dav_peer:
            dav_peer=dav_test.Apache('browser-dav-peer')
            dav_peer.config.write_text(dav_peer.config.read_text(encoding='utf-8').replace('<Location /svn>','<Location />'),encoding='utf-8')
    for n in (a,b):
        with n.config.open("a",encoding="utf-8") as out: out.write("node-id = "+n.name+"\n")
    state={"url":f"http://127.0.0.1:{a.monitor_port}/monitor/","database":setup.DATABASE,
        "repository_id":repo["id"],"token_id":issued["id"],"work":str(setup.WORK),
        "control":str(setup.WORK/"browser-control.json"),"session":str(setup.WORK/"browser-session.json"),
        "pgvnserve_sha256":hashlib.sha256((RUNTIME/"bin/pgvnserve.exe").read_bytes()).hexdigest(),
        "assets":{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (ROOT/"web/pgvn-monitor").iterdir() if f.is_file()}}
    blocked=False; last_sequence=0
    try:
        a.start(); b.start(); until(lambda:ready_status(a)==200)
        content=setup.WORK/"readme.txt"; content.write_text("PgVN 浏览器验收\n",encoding="utf-8")
        client("svnmucc","-m","browser history","put",content,a.url("/ui-demo/readme.txt"))
        client("svn","lock",a.url("/ui-demo/readme.txt"),"-m","browser lock")
        client("svnmucc","-m","denied","mkdir",a.url("/ui-demo/forbidden"),expected=1)
        setup.run(setup.PROBE,["begin",repo["id"],"writer"],role="pgvn_service")
        until(lambda:len(__import__('test_m4_smoke').request(a,"/api/monitor/v1/nodes",issued["token"])[0]["items"])==2)
        if dav:
            dav.start();dav_test.svn('info',dav.url('/ui-demo'))
            status,headers,body=dav.request('POST','/ui-demo/!svn/me',b'( create-txn )',{'Content-Type':'application/vnd.svn-skel'},user='reader')
            assert status==403,(status,body)
            state['dav_error_request_id']=headers['X-PgVN-Request-Id']
            until(lambda:len(__import__('test_m4_smoke').request(a,'/api/monitor/v1/nodes',issued['token'])[0]['items'])==3)
            state.update(dav_pid=dav.process.pid,dav_node_id='dav-browser-dav',
                dav_modules={name:hashlib.sha256((dav_test.APACHE/'modules'/name).read_bytes()).hexdigest() for name in ('mod_dav_pgvn.so','mod_authz_pgvn.so')})
            state['dav_host_sha256']=hashlib.sha256(dav.executable.read_bytes()).hexdigest()
            state['dav_host_runtime']=json.loads(dav_test.run(dav.executable,'--pgvn-runtime'))
        if dav_peer:
            dav_peer.start();dav_test.svn('info',dav_peer.url('/ui-demo'))
            status,headers,body=dav_peer.request('POST','/ui-demo/!svn/me',b'( create-txn )',{'Content-Type':'application/vnd.svn-skel'},user='reader')
            assert status==403,(status,body)
            until(lambda:len(__import__('test_m4_smoke').request(a,'/api/monitor/v1/nodes',issued['token'])[0]['items'])==4)
            state['dav_peer']={'pid':dav_peer.process.pid,'node_id':'dav-browser-dav-peer','error_request_id':headers['X-PgVN-Request-Id']}
        state.update(pid=a.process.pid,peer_pid=b.process.pid,sequence=0,action="started",status="running")
        (setup.WORK/"browser-session.json").write_text(json.dumps(state),encoding="utf-8")
        print(json.dumps(dict(state,token=issued["token"])),flush=True)
        deadline=time.monotonic()+900
        while time.monotonic()<deadline:
            control=setup.WORK/"browser-control.json"
            if control.exists():
                try: command=json.loads(control.read_text(encoding="utf-8-sig"))
                except (OSError,ValueError): time.sleep(.1); continue
                if command["sequence"]>last_sequence:
                    last_sequence=command["sequence"]; action=command["action"]
                    if action=="stop": break
                    if action=="stop-peer": b.stop()
                    elif action=="start-peer": b.start()
                    elif action=='stop-dav':
                        if not dav:raise ValueError('DAV fixture is not enabled')
                        dav.close()
                    elif action=='start-dav':
                        if not dav:raise ValueError('DAV fixture is not enabled')
                        dav.start();dav_test.svn('info',dav.url('/ui-demo'));state['dav_pid']=dav.process.pid
                    elif action=="block-db":
                        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS false",database="postgres"); blocked=True
                        setup.fixture.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{setup.DATABASE}'",database="postgres")
                    elif action=="restore-db":
                        setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS true",database="postgres"); blocked=False
                    elif action=="revoke-token": setup.admin("monitor-token","revoke",issued["id"])
                    else: raise ValueError("Unknown browser fixture action")
                    state.update(sequence=last_sequence,action=action)
                    (setup.WORK/"browser-session.json").write_text(json.dumps(state),encoding="utf-8")
            time.sleep(.2)
    finally:
        if dav_peer:dav_peer.close()
        if dav:dav.close()
        if blocked: setup.fixture.sql(f"ALTER DATABASE {setup.DATABASE} ALLOW_CONNECTIONS true",database="postgres")
        a.stop(); b.stop(); setup.admin("monitor-token","revoke",issued["id"])
        state["status"]="stopped"
        (setup.WORK/"browser-session.json").write_text(json.dumps(state),encoding="utf-8")


if __name__=="__main__": main()
