"""Real Apache PgVN DAV client checks, in a dedicated catalog and private ports."""
from pathlib import Path
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
import test_m3_credentials as setup
from fixture import ROOT,RUNTIME

TEST_ID=setup.TEST_ID.replace('m3-credentials','m5-http')
APACHE=RUNTIME/'apache'
SVN=RUNTIME/'bin/svn.exe'
PASSWORD=secrets.token_urlsafe(24)
records={}


def run(executable,*args,input=None,expected=0,timeout=40):
    result=subprocess.run([str(executable),*map(str,args)],input=input,cwd=setup.EMPTY,
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=timeout)
    if result.returncode!=expected:
        # Do not serialize authentication arguments or the global config.
        raise AssertionError((str(executable),result.returncode,result.stdout[:2000],result.stderr[:4000]))
    return result.stdout


def svn(*args,user='writer',expected=0):
    return run(SVN,'--non-interactive','--no-auth-cache','--username',user,'--password',PASSWORD,*args,expected=expected)


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


class Apache:
    def __init__(self,name,v2=True):
        self.work=setup.WORK/name;self.work.mkdir();self.port=port();self.process=None;self.log=None
        self.config=self.work/'httpd.conf'
        native=self.work/'pgvn.ini'
        native.write_text('[pgvn]\nconnection = '+setup.dsn('pgvn_service')+'\nnode-id = dav-'+name+'\n',encoding='utf-8')
        passwd=self.work/'users.htpasswd'
        for index,user in enumerate(('writer','other','reader')):
            run(APACHE/'bin/htpasswd.exe',*(['-c'] if index==0 else []),'-i',passwd,user,input=(PASSWORD+'\n').encode())
        modules=['authn_core','authn_file','auth_basic','authz_core','authz_user','dav','log_config','headers']
        text='ServerRoot "'+APACHE.as_posix()+'"\n'
        text+='Listen 127.0.0.1:'+str(self.port)+'\nServerName 127.0.0.1\nThreadsPerChild 16\n'
        for name in modules:text+='LoadModule '+name+'_module modules/mod_'+name+'.so\n'
        text+='LoadModule dav_pgvn_module modules/mod_dav_pgvn.so\nLoadModule authz_pgvn_module modules/mod_authz_pgvn.so\n'
        text+='ErrorLog "'+(self.work/'error.log').as_posix()+'"\nLogLevel warn\n'
        text+='PidFile "'+(self.work/'httpd.pid').as_posix()+'"\n'
        text+='LogFormat "%m %U %>s" pgvn\nCustomLog "'+(self.work/'access.log').as_posix()+'" pgvn\n'
        text+='PgVNConfig "'+native.as_posix()+'"\n'
        text+='Header always set X-PgVN-Test-Node "'+self.work.name+'"\n'
        text+='<Location /svn>\nDAV pgvn\nPgVNPathAuthz short_circuit\nPgVNAdvertiseV2Protocol '+('On' if v2 else 'Off')+'\n'
        text+='AuthType Basic\nAuthName "PgVN isolated test"\nAuthBasicProvider file\nAuthUserFile "'+passwd.as_posix()+'"\nRequire valid-user\n</Location>\n'
        self.config.write_text(text,encoding='utf-8')

    def start(self):
        run(APACHE/'bin/httpd.exe','-t','-f',self.config)
        self.log=(self.work/'launcher.log').open('ab')
        self.process=subprocess.Popen([str(APACHE/'bin/httpd.exe'),'-X','-f',str(self.config)],cwd=setup.EMPTY,
            stdout=self.log,stderr=self.log,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.process.poll() is not None:
                error=(self.work/'error.log')
                raise AssertionError('Apache exited: '+(error.read_text(errors='replace')[-4000:] if error.exists() else 'see launcher'))
            try:
                with socket.create_connection(('127.0.0.1',self.port),timeout=.2):return self
            except OSError:time.sleep(.1)
        raise AssertionError('Apache did not listen')

    def enable_tls(self,certificate,key):
        self.tls_port=port()
        with self.config.open('a',encoding='utf-8') as stream:
            stream.write('LoadModule socache_shmcb_module modules/mod_socache_shmcb.so\nLoadModule ssl_module modules/mod_ssl.so\n'
                'SSLSessionCache none\nListen 127.0.0.1:'+str(self.tls_port)+'\n<VirtualHost 127.0.0.1:'+str(self.tls_port)+'>\n'
                'ServerName localhost\nSSLEngine On\nSSLCertificateFile "'+certificate.as_posix()+'"\n'
                'SSLCertificateKeyFile "'+key.as_posix()+'"\n</VirtualHost>\n')

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        if self.log:self.log.close()

    def url(self,path):return 'http://127.0.0.1:'+str(self.port)+urllib.parse.quote(path,safe='/!')

    def request(self,method,path,body=None,headers=None,user='writer'):
        h=dict(headers or {})
        if user:h['Authorization']='Basic '+base64.b64encode((user+':'+PASSWORD).encode()).decode()
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        try:
            connection.request(method,urllib.parse.quote(path,safe='/!'),body=body,headers=h)
            response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:connection.close()


def checks():
    setup.setup_database()
    route='/svn/团队/project'
    repo=setup.admin('repo','create','canonical-http','--route',route)
    rules=setup.WORK/'authz.conf';rules.write_text('[canonical-http:/]\nwriter = rw\nother = rw\nreader = r\n测试用户 = rw\n',encoding='utf-8')
    hooks=setup.WORK/'hooks.json';hooks.write_text('{"pre-revprop-change":"allow"}')
    setup.admin('config','publish',repo['id'],'--expected-generation',1,'--authz',rules,'--hooks',hooks)
    server=Apache('v2')
    users=server.work/'users.htpasswd';writer_hash=users.read_text(encoding='utf-8').splitlines()[0].split(':',1)[1]
    with users.open('a',encoding='utf-8') as stream:stream.write('测试用户:'+writer_hash+'\n')
    try:
        server.start();url=server.url(route)
        assert server.request('OPTIONS',route,user=None)[0]==401
        assert 'repository' in svn('info','--xml',url).decode()
        svn('mkdir',url+'/trunk','-m','HTTP r1')
        wc=setup.WORK/'wc';svn('checkout',url+'/trunk',wc)
        binary=wc/'二进制 %.bin';payload=b'HTTP DAV\x00\xff\r\n'+bytes(range(256));binary.write_bytes(payload)
        text=wc/'text.txt';text.write_text('first line\nsecond line\n',encoding='utf-8')
        svn('add',binary,text);svn('commit',wc,'-m','HTTP r2')
        svn('update',wc)
        assert svn('cat',url+'/trunk/'+urllib.parse.quote(binary.name))==payload
        props=setup.WORK/'prop.bin';props.write_bytes(b'property\0\xff')
        svn('propset','test:binary','--file',props,binary)
        svn('propset','test:inherited','root-value',wc)
        svn('commit',wc,'-m','HTTP r3')
        assert svn('propget','--strict','test:binary',url+'/trunk/'+urllib.parse.quote(binary.name))==props.read_bytes()
        assert b'root-value' in svn('propget','--show-inherited-props','test:inherited',url+'/trunk/text.txt')
        svn('copy',url+'/trunk',url+'/branch','-m','HTTP copy r4')
        assert svn('cat',url+'/branch/'+urllib.parse.quote(binary.name))==payload
        assert b'copyfrom-path' in svn('log','--xml','-v',url)
        assert b'first line' in svn('blame',url+'/trunk/text.txt')
        svn('lock',url+'/trunk/text.txt','-m','HTTP lock')
        assert b'<lock>' in svn('info','--xml',url+'/trunk/text.txt')
        svn('unlock',url+'/trunk/text.txt')
        svn('propset','--revprop','-r','4','svn:log','revised log',url)
        assert b'revised log' in svn('propget','--revprop','-r','4','svn:log',url)
        svn('mkdir',url+'/unicode-user','-m','Unicode author r5',user='测试用户')
        assert ET.fromstring(svn('log','--xml','-r','5',url)).find('logentry/author').text=='测试用户'
        assert setup.sql("SELECT encode(value,'hex') FROM pgfs.revision_properties WHERE rev=5 AND name='svn:author'")=='测试用户'.encode('utf-8').hex()
        # Protocol v2 virtual binding is distinct from activity and native IDs.
        status,headers,body=server.request('POST',route+'/!svn/me',b'( create-txn )',
            {'Content-Type':'application/vnd.svn-skel','SVN-VTxn-Name':'explicit-virtual'})
        assert status==201,(status,body)
        assert headers.get('SVN-VTxn-Name')=='explicit-virtual',headers
        status,_,body=server.request('DELETE',route+'/!svn/vtxn/explicit-virtual',user='other')
        assert status in (403,404),(status,body)
        assert server.request('DELETE',route+'/!svn/vtxn/explicit-virtual')[0]==204
        assert setup.admin('look','youngest',repo['id'])['youngest']==5
        records['http_v2']={'standard_client_binary_history_props_copy_locks_revprops':True,
            'unicode_route_and_percent_filename':True,'canonical_authz_name_distinct_from_route':True,
            'virtual_owner_and_delete':True,'unicode_authenticated_author_preserved':True}
    finally:server.close()
    legacy=Apache('v1',v2=False)
    try:
        legacy.start();url=legacy.url(route)
        svn('mkdir',url+'/v1','-m','HTTP v1 r6')
        assert setup.sql("SELECT count(*) FROM pgvn.dav_bindings WHERE binding_kind='activity'")!='0'
        assert setup.admin('look','youngest',repo['id'])['youngest']==6
        assert b'<logentry' in svn('log','--xml',url,user='reader')
        denied=run(SVN,'--non-interactive','--no-auth-cache','--username','reader','--password',PASSWORD,
            'mkdir',url+'/denied','-m','must fail',expected=1)
        assert setup.admin('look','youngest',repo['id'])['youngest']==6
        records['http_v1']={'standard_client_activity_commit':True,'reader_read_and_write_denial':True}
    finally:legacy.close()
    setup.admin('verify',repo['id']);records['full_verify']=True
    return records


def main():
    start=time.monotonic()
    try:details=checks();status='PASS';error=None
    except Exception:details=records;status='FAIL';error=traceback.format_exc();print(error,flush=True)
    report={'test_id':TEST_ID,'component':'M5 real Apache HTTP','status':status,'milestone_complete':False,
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'details':details,'error':error,
        'elapsed_seconds':time.monotonic()-start,'modules':{n:hashlib.sha256((APACHE/'modules'/n).read_bytes()).hexdigest()
            for n in ('mod_dav_pgvn.so','mod_authz_pgvn.so')}}
    output=ROOT/'artifacts/pgvn/tests';output.mkdir(parents=True,exist_ok=True);data=json.dumps(report,indent=2)+'\n'
    (output/(TEST_ID+'.json')).write_text(data,encoding='utf-8');(output/'m5-http-latest.json').write_text(data,encoding='utf-8')
    print(json.dumps(report),flush=True);return 0 if status=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
