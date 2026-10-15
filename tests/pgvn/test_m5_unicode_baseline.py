"""Reproduce the installed SVNKit Unicode revprop limitation on PgVN and stock SVN."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import shutil
import time
import traceback
from test_m5_http import Apache,APACHE,run,svn,setup,ROOT,RUNTIME,PASSWORD
from svnkit_client import SvnKit

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-unicode-baseline')
records={}


def checks():
    setup.setup_database();route='/svn/团队'
    native=setup.admin('repo','create','unicode-native','--route',route)
    rules=setup.WORK/'authz.conf';rules.write_text('[/]\nwriter = rw\n',encoding='utf-8')
    hooks=setup.WORK/'hooks.json';hooks.write_text('{"pre-revprop-change":"allow"}')
    setup.admin('config','publish',native['id'],'--expected-generation',1,'--authz',rules,'--hooks',hooks)
    servers=[Apache('unicode-native',v2=False),Apache('unicode-stock',v2=False,executable=APACHE/'bin/httpd.exe')]
    stock=servers[1];modules=stock.work/'modules';modules.mkdir()
    sdk=Path(json.loads((ROOT/'config/build.local.json').read_text())['apache'])
    stock_hashes={}
    for name in ('mod_dav_svn.so','mod_authz_svn.so'):
        source=sdk/'modules'/name;shutil.copy2(source,modules/name);stock_hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    parent=stock.work/'repos';parent.mkdir();fsfs=parent/'团队';run(RUNTIME/'bin/svnadmin.exe','create',fsfs)
    (fsfs/'hooks/pre-revprop-change.bat').write_text('@exit /b 0\n',encoding='ascii')
    text=stock.config.read_text(encoding='utf-8')
    text=text.replace('LoadModule dav_pgvn_module modules/mod_dav_pgvn.so','LoadModule dav_svn_module "'+(modules/'mod_dav_svn.so').as_posix()+'"')
    text=text.replace('LoadModule authz_pgvn_module modules/mod_authz_pgvn.so','LoadModule authz_svn_module "'+(modules/'mod_authz_svn.so').as_posix()+'"')
    text='\n'.join(line for line in text.splitlines() if not line.startswith(('PgVNConfig','PgVNPathAuthz','PgVNAdvertiseV2Protocol')))+'\n'
    text=text.replace('DAV pgvn','DAV svn\nSVNParentPath "'+parent.as_posix()+'"\nAuthzSVNAccessFile "'+rules.as_posix()+'"\nSVNAdvertiseV2Protocol Off')
    stock.config.write_text(text,encoding='utf-8')
    try:
        for server in servers:
            server.start();url=server.url(route)
            svn('mkdir',url+'/trunk','-m','baseline r1');wc=server.work/'wc';svn('checkout',url+'/trunk',wc)
            payload=wc/'payload.bin';payload.write_bytes(b'baseline\0\xff');svn('add',payload);svn('commit',wc,'-m','baseline r2')
            client_work=server.work/'client';client_work.mkdir()
            client=SvnKit(client_work,(url,url),'writer',PASSWORD.encode())
            try:
                client.roundtrip(payload)
            except AssertionError as error:
                message=str(error);assert 'E175002' in message and 'E175008' in message,message
            else:raise AssertionError('Expected the pinned upstream SVNKit limitation; reassess the documented compatibility boundary')
            # The same client already read binary data and committed r3. Its
            # subsequent PROPPATCH has %25 in the wire path: Apache's once-
            # decoded access log still contains literal percent-encoded UTF-8.
            assert b'SVNKit commit' in svn('propget','--revprop','-r','3','svn:log',url)
            lines=(server.work/'access.log').read_text(errors='replace').splitlines()
            failed=[line for line in lines if line.startswith('PROPPATCH /svn/%E5%9B%A2%E9%98%9F/') and line.endswith(' 404')]
            assert failed,lines[-10:]
            records[server.work.name]={'binary_read_and_commit_before_revprop_failure':True,
                'revprop_error_codes':[175002,175008],'once_decoded_log_retains_percent_encoded_repository':failed[-1],
                'client':client.evidence}
        setup.admin('verify',native['id']);run(RUNTIME/'bin/svnadmin.exe','verify',fsfs,'--quiet')
        records['stock_module_sha256']=stock_hashes
        records['scope']='Expected limitation reproduced with installed SVNKit 1.10.1 on both servers; full SVNKit roundtrip uses ASCII routes in the balancer gate. Standard native SVN Unicode operations are covered independently. No server-side second URL decode is introduced.'
        return records
    finally:
        for server in servers:server.close()


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 pinned Unicode client baseline','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,'elapsed_seconds':time.monotonic()-start,
        'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest() for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m5-unicode-baseline-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='details'}),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
