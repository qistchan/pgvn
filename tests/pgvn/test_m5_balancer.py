"""Real Apache round robin, process loss, receipt retry, HTTPS and SVNKit."""
from pathlib import Path
import base64
import datetime as dt
import hashlib
import http.client
import json
import ssl
import subprocess
import time
import traceback
import urllib.parse
from xml.sax.saxutils import escape
from test_m5_http import Apache,APACHE,SVN,run,svn,port,setup,PASSWORD,ROOT,RUNTIME
from svnkit_client import SvnKit

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-balancer')
records={}


class Balancer(Apache):
    def __init__(self,a,b):
        self.work=setup.WORK/'balancer';self.work.mkdir();self.port=port();self.tls_port=port();self.process=None;self.log=None
        self.config=self.work/'httpd.conf';self.cert=self.work/'ca.pem';self.key=self.work/'server.key'
        openssl=self.work/'openssl.cnf'
        ca_key=self.work/'ca.key';server_cert=self.work/'server.pem';csr=self.work/'server.csr'
        openssl.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n'
            '[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n'
            'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
            'extendedKeyUsage=serverAuth\n',encoding='ascii')
        run(APACHE/'bin/openssl.exe','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
            '-config',openssl,'-keyout',ca_key,'-out',self.cert)
        extensions=self.work/'server.cnf'
        extensions.write_text('[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=localhost\n'
            '[server]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n',encoding='ascii')
        run(APACHE/'bin/openssl.exe','req','-new','-newkey','rsa:2048','-nodes','-config',extensions,'-keyout',self.key,'-out',csr)
        run(APACHE/'bin/openssl.exe','x509','-req','-in',csr,'-CA',self.cert,'-CAkey',ca_key,'-CAcreateserial',
            '-days','2','-extfile',extensions,'-extensions','server','-out',server_cert)
        for backend in (a,b):backend.enable_tls(server_cert,self.key)
        modules=['authz_core','log_config','proxy','proxy_http','slotmem_shm','proxy_balancer','lbmethod_byrequests','socache_shmcb','ssl']
        text='ServerRoot "'+APACHE.as_posix()+'"\nServerName localhost\nListen 127.0.0.1:'+str(self.port)+'\n'
        text+='Listen 127.0.0.1:'+str(self.tls_port)+'\nThreadsPerChild 16\n'
        for name in modules:text+='LoadModule '+name+'_module modules/mod_'+name+'.so\n'
        text+='ErrorLog "'+(self.work/'error.log').as_posix()+'"\nLogLevel warn\n'
        text+='PidFile "'+(self.work/'httpd.pid').as_posix()+'"\n'
        text+='LogFormat "%m %U %>s" pgvn\nCustomLog "'+(self.work/'access.log').as_posix()+'" pgvn\n'
        text+='Timeout 15\nProxyTimeout 10\nProxyRequests Off\nProxyPreserveHost On\n'
        text+='SSLSessionCache none\n'
        text+='<Proxy balancer://pgvn>\n'
        for backend in (a,b):text+='BalancerMember http://127.0.0.1:'+str(backend.port)+' retry=1 connectiontimeout=2\n'
        text+='ProxySet lbmethod=byrequests maxattempts=1\n</Proxy>\n'
        text+='ProxyPass /svn balancer://pgvn/svn nocanon\nProxyPassReverse /svn balancer://pgvn/svn\n'
        text+='<Location /svn>\nRequire all granted\n</Location>\n'
        text+='<VirtualHost 127.0.0.1:'+str(self.tls_port)+'>\nServerName localhost\nSSLEngine On\nSSLProtocol -all +TLSv1.2 +TLSv1.3\n'
        text+='SSLCertificateFile "'+server_cert.as_posix()+'"\nSSLCertificateKeyFile "'+self.key.as_posix()+'"\n'
        text+='SSLProxyEngine On\nSSLProxyVerify require\nSSLProxyCheckPeerName On\nSSLProxyCACertificateFile "'+self.cert.as_posix()+'"\n'
        text+='ProxyPassInherit Off\n<Proxy balancer://pgvn_tls>\n'
        for backend in (a,b):text+='BalancerMember https://127.0.0.1:'+str(backend.tls_port)+' retry=1 connectiontimeout=2\n'
        text+='ProxySet lbmethod=byrequests maxattempts=1\n</Proxy>\nProxyPass /svn balancer://pgvn_tls/svn nocanon\n'
        text+='ProxyPassReverse /svn balancer://pgvn_tls/svn\n</VirtualHost>\n'
        self.config.write_text(text,encoding='utf-8')

    def url(self,path,tls=False):
        return ('https://localhost:'+str(self.tls_port) if tls else 'http://127.0.0.1:'+str(self.port))+urllib.parse.quote(path,safe='/!')

    def request(self,method,path,body=None,headers=None,tls=False,user='writer'):
        h=dict(headers or {});h['Authorization']='Basic '+base64.b64encode((user+':'+PASSWORD).encode()).decode()
        c=(http.client.HTTPSConnection('localhost',self.tls_port,timeout=15,context=ssl.create_default_context(cafile=str(self.cert)))
            if tls else http.client.HTTPConnection('127.0.0.1',self.port,timeout=15))
        try:
            c.request(method,urllib.parse.quote(path,safe='/!'),body=body,headers=h)
            r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
        finally:c.close()


def merge_body(href):
    return ('<D:merge xmlns:D="DAV:"><D:source><D:href>'+escape(href)+
        '</D:href></D:source><D:no-auto-merge/><D:no-checkout/><D:prop><D:checked-in/>'
        '<D:version-name/><D:creationdate/><D:creator-displayname/></D:prop></D:merge>').encode()


def checks():
    setup.setup_database();route='/svn/balanced'
    repo=setup.admin('repo','create','balanced-canonical','--route',route)
    authz=setup.WORK/'authz.conf';authz.write_text('[balanced-canonical:/]\nwriter = rw\n',encoding='utf-8')
    hooks=setup.WORK/'hooks.json';hooks.write_text('{"pre-revprop-change":"allow"}')
    setup.admin('config','publish',repo['id'],'--expected-generation',1,'--authz',authz,'--hooks',hooks)
    a=Apache('node-a');b=Apache('node-b');proxy=None
    try:
        proxy=Balancer(a,b);a.start();b.start();proxy.start();url=proxy.url(route)
        observed=[]
        for _ in range(6):
            status,headers,body=proxy.request('OPTIONS',route)
            assert status==200,(status,body);observed.append(headers['X-PgVN-Test-Node'])
        assert all(observed[i]!=observed[i-1] for i in range(1,len(observed))),observed
        svn('mkdir',url+'/trunk','-m','balanced r1')
        wc=setup.WORK/'wc';svn('checkout',url+'/trunk',wc)
        content=wc/'payload.bin';payload=bytes(range(256))*1024+b'\x00\xff';content.write_bytes(payload)
        literal=wc/'literal%2F.txt';literal.write_bytes(b'percent path')
        svn('add',content,literal);svn('commit',wc,'-m','balanced r2')
        assert svn('cat',url+'/trunk/payload.bin')==payload
        assert svn('cat',url+'/trunk/literal%252F.txt')==literal.read_bytes()
        # A real SVNKit client shares history through the same nonsticky front.
        java=SvnKit(setup.WORK,(url,url),'writer',PASSWORD.encode())
        result=java.roundtrip(content);assert result['roundtrip'];records['svnkit_http']=dict(result,**java.evidence)
        # Create directly on A, then continue after A exits through the front.
        before=setup.admin('look','youngest',repo['id'])['youngest'];alias='creator-loss-virtual'
        status,_,body=a.request('POST',route+'/!svn/me',b'( create-txn-with-props (svn:log failover) )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':alias})
        assert status==201,(status,body);a.close()
        status,headers,body=proxy.request('PUT',route+'/!svn/vtxr/'+alias+'/after-loss.bin',b'after creator loss\x00\xff',
            {'Content-Type':'application/octet-stream'})
        assert status in (201,204) and headers['X-PgVN-Test-Node']=='node-b',(status,body,headers)
        merge=merge_body(proxy.url(route+'/!svn/vtxn/'+alias))
        for _ in range(2):
            status,headers,body=proxy.request('MERGE',route,merge,{'Content-Type':'text/xml'})
            assert status==200,(status,body)
            assert setup.admin('look','youngest',repo['id'])['youngest']==before+1
        assert svn('cat',url+'/after-loss.bin')==b'after creator loss\x00\xff'
        records['creator_process_loss']={'created_on':'node-a','continued_on':'node-b','repeated_merge_same_revision':before+1}
        a.start();time.sleep(1.1)
        https=proxy.url(route,tls=True)
        trust=['--config-option','servers:global:ssl-authority-files='+str(proxy.cert)]
        assert svn(*trust,'cat',https+'/trunk/payload.bin')==payload
        svn(*trust,'mkdir',https+'/tls','-m','verified HTTPS')
        assert proxy.request('OPTIONS',route,tls=True)[0]==200
        records['https']={'native_apache_tls':True,'client_trusts_fixture_ca':True,'proxy_verifies_backend_certificate':True,'read_and_commit':True}
        tls_work=setup.WORK/'tls-client';tls_work.mkdir();trust_dir=tls_work/'svnkit-config';trust_dir.mkdir()
        (trust_dir/'servers').write_text('[global]\nssl-authority-files = '+proxy.cert.as_posix()+'\nssl-trust-default-ca = false\n',encoding='utf-8')
        tls_java=SvnKit(tls_work,(https,https),'writer',PASSWORD.encode())
        tls_result=tls_java.roundtrip(content,directory='java-https');assert tls_result['roundtrip']
        records['svnkit_https']=dict(tls_result,trusted_fixture_ca=True)
        # Repeat a standard client commit over DeltaV activities, alternating
        # requests across two freshly started Apache processes.
        for backend in (a,b):
            backend.close();text=backend.config.read_text(encoding='utf-8')
            backend.config.write_text(text.replace('PgVNAdvertiseV2Protocol On','PgVNAdvertiseV2Protocol Off'),encoding='utf-8')
            backend.start()
        time.sleep(1.1);svn('mkdir',url+'/v1-balanced','-m','nonsticky activity commit')
        assert setup.sql("SELECT count(*) FROM pgvn.dav_bindings WHERE binding_kind='activity'")!='0'
        requests={}
        for backend in (a,b):
            rows=(backend.work/'access.log').read_text(errors='replace').splitlines()
            methods=[row.split(' ',1)[0] for row in rows]
            assert 'MERGE' in methods and ('POST' in methods or 'PUT' in methods),methods
            requests[backend.work.name]={'requests':len(rows),'methods':sorted(set(methods))}
        records['round_robin']={'six_options_alternate':observed,'backend_logs':requests,'v1_commit':True,
            'v2_binary_commit':True,'single_url_decode_through_proxy':True,'scope':'Two local independent Apache processes; no cross-host failover claim.'}
        setup.admin('verify',repo['id']);records['full_verify']=True
        return records
    finally:
        if proxy:proxy.close()
        a.close();b.close()


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 real Apache HTTP/HTTPS round robin','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,
        'elapsed_seconds':time.monotonic()-start,'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest()
            for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    out=ROOT/'artifacts/pgvn/tests';out.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (out/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(out/'m5-balancer-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
