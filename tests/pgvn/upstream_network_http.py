"""Private loopback Apache configurations for original upstream network tests.

No installed server/configuration is changed. The control uses pinned stock
DAV providers; native mode uses the production PgVN modules. TLS trust stays
in each test client's configuration, with a short-lived private CA.
"""
from pathlib import Path
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/'runtime/pgvn'
APACHE=RUNTIME/'apache'
SELF='tests/pgvn/upstream_network_http.py'
STOCK_PROVIDERS={
    'mod_dav_svn.so':'f7c580dfc0c41cc9a3655c1e03688731dfdbabd00cf55971aede51b5472a9121',
    'mod_authz_svn.so':'3633412c666240517a7e4205bfa1b77b0b28c08948acd20033d6ba0d6f824d48',
}
COMMON_MODULES=('authn_core','authn_file','auth_basic','authz_core','authz_user','dav','log_config','headers')
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()

def identity(manifest):
    assert sha(ROOT/SELF)==manifest['sources'][SELF],'Unregistered HTTP fixture input'
    sdk=Path(manifest['inputs']['apache']);stock={}
    for name,expected in STOCK_PROVIDERS.items():
        source=sdk/'modules'/name;assert sha(source)==expected,'Changed original control provider: '+name
        stock[name]={'path':str(source),'sha256':expected}
    dependencies={}
    for name,expected in manifest['apache_dependencies'].items():
        assert sha(RUNTIME/name)==expected,name
        dependencies[name]=expected
    for name in ('runtime/pgvn/apache/bin/pgvnhttpd.exe','runtime/pgvn/apache/modules/mod_dav_pgvn.so',
                 'runtime/pgvn/apache/modules/mod_authz_pgvn.so'):
        assert sha(ROOT/name)==manifest['products'][name]['sha256']
    return {'source_sha256':sha(ROOT/SELF),'stock_providers':stock,
            'apache_dependencies':dependencies,'apache_version':manifest['apache_host']['version'],
            'native_products':{name:record['sha256'] for name,record in manifest['products'].items() if '/apache/' in name}}

def run(program,args,work,env,input=None):
    result=subprocess.run([str(program),*map(str,args)],cwd=work,env=env,input=input,
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=45)
    assert result.returncode==0,(Path(program).name,result.returncode,result.stderr[-3000:])
    return result

def tls_material(work,env):
    work=work.resolve();assert work.is_relative_to((RUNTIME/'tests').resolve())
    target=work/'tls';target.mkdir(exist_ok=False)
    authority=target/'ca.pem';authority_key=target/'ca.key';certificate=target/'server.pem'
    key=target/'server.key';request=target/'server.csr'
    ca_config=target/'ca.cnf'
    ca_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n'
        '[dn]\nCN=PgVN M6 isolated CA\n[extensions]\nbasicConstraints=critical,CA:TRUE\n'
        'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n',encoding='ascii')
    server_config=target/'server.cnf'
    server_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=localhost\n'
        '[server]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n',encoding='ascii')
    openssl=APACHE/'bin/openssl.exe'
    run(openssl,['req','-x509','-newkey','rsa:2048','-nodes','-days','2','-config',ca_config,
                 '-keyout',authority_key,'-out',authority],target,env)
    run(openssl,['req','-new','-newkey','rsa:2048','-nodes','-config',server_config,
                 '-keyout',key,'-out',request],target,env)
    run(openssl,['x509','-req','-in',request,'-CA',authority,'-CAkey',authority_key,'-CAcreateserial',
                 '-days','2','-extfile',server_config,'-extensions','server','-out',certificate],target,env)
    run(openssl,['verify','-CAfile',authority,'-verify_ip','127.0.0.1','-purpose','sslserver',certificate],target,env)
    return {'authority':str(authority),'certificate':str(certificate),'key':str(key),
            'authority_sha256':sha(authority),'certificate_sha256':sha(certificate),
            'scope':'Private two-day CA/server certificate; no machine trust store changes.'}

def verify_tls(number,tls):
    resolved=sorted({entry[4][0] for entry in socket.getaddrinfo('localhost',number,type=socket.SOCK_STREAM)})
    assert resolved and all(ipaddress.ip_address(address).is_loopback for address in resolved),resolved
    failures=[]
    for context,hostname,label in ((ssl.create_default_context(),'127.0.0.1','untrusted CA'),
                                   (ssl.create_default_context(cafile=tls['authority']),'wrong.invalid','wrong hostname')):
        try:
            with socket.create_connection(('127.0.0.1',number),timeout=5) as raw:
                with context.wrap_socket(raw,server_hostname=hostname):pass
        except ssl.SSLCertVerificationError as error:failures.append({'case':label,'verify_code':error.verify_code})
        else:raise AssertionError('TLS negative control unexpectedly accepted: '+label)
    verified=[];endpoints=[];result={}
    for address,hostname in (('127.0.0.1','127.0.0.1'),('localhost','localhost'),('::1','localhost')):
        context=ssl.create_default_context(cafile=tls['authority'])
        start=time.monotonic()
        with socket.create_connection((address,number),timeout=5) as raw:
            elapsed=time.monotonic()-start
            peer_address=raw.getpeername()[0]
            assert ipaddress.ip_address(peer_address).is_loopback
            with context.wrap_socket(raw,server_hostname=hostname) as secure:
                peer=secure.getpeercert(binary_form=True)
                expected=ssl.PEM_cert_to_DER_cert(Path(tls['certificate']).read_text(encoding='ascii'))
                assert peer==expected;verified.append(hostname)
                endpoints.append({'connect_host':address,'peer_address':peer_address,'verified_hostname':hostname,
                                  'tcp_connect_seconds':round(elapsed,6)})
                result={'version':secure.version(),'cipher':secure.cipher()[0],
                        'peer_certificate_der_sha256':hashlib.sha256(peer).hexdigest()}
    return dict(result,hostname_and_chain_verified=True,verified_names=list(dict.fromkeys(verified)),
                verified_endpoints=endpoints,negative_controls=failures,
                client_url_hostname='localhost',localhost_resolves_to=resolved)

def configure(area,backend,number,database,domain,manifest,env,tls=None):
    area=area.resolve();assert area.is_relative_to((RUNTIME/'tests').resolve())
    assert backend in ('pgvn','fsfs') and 1024<=number<=65535
    assert re.fullmatch('pgvn_m6_net_[0-9a-f]{12}',database)
    assert domain=='upstream-'+database.removeprefix('pgvn_m6_net_')
    pinned=identity(manifest)
    password=area/'users.htpasswd'
    for index,user in enumerate(('jrandom','jconstant')):
        run(APACHE/'bin/htpasswd.exe',[*(['-c'] if index==0 else []),'-i',password,user],area,env,input=b'rayjandom\n')
    text='ServerRoot "'+APACHE.as_posix()+'"\n'
    text+='Listen 127.0.0.1:'+str(number)+'\n'
    if tls:text+='Listen [::1]:'+str(number)+'\n'
    text+='ServerName '+('localhost' if tls else '127.0.0.1')+'\nThreadsPerChild 16\n'
    for name in COMMON_MODULES:text+='LoadModule '+name+'_module modules/mod_'+name+'.so\n'
    text+='ErrorLog "'+(area/'error.log').as_posix()+'"\nLogLevel warn\n'
    text+='PidFile "'+(area/'httpd.pid').as_posix()+'"\n'
    text+='LogFormat "%m %U %>s" m6\nCustomLog "'+(area/'access.log').as_posix()+'" m6\n'
    public=area/'public';public.mkdir(exist_ok=False)
    text+='Timeout 30\nDocumentRoot "'+public.as_posix()+'"\n'
    text+='<Directory "'+public.as_posix()+'">\nRequire all granted\n</Directory>\n'
    auth='AuthType Basic\nAuthName "Subversion M6 isolated tests"\nAuthBasicProvider file\n'
    auth+='AuthUserFile "'+password.as_posix()+'"\nRequire valid-user\n'
    if backend=='pgvn':
        native=area/'pgvn.ini'
        native.write_text('[pgvn]\nconnection = host=127.0.0.1 port=16432 dbname='+database+
            ' user=pgvn_service sslmode=disable\nnode-id = m6-dav-'+area.parent.name+\
            '\nidentity-domain = '+domain+'\nhook-root = '+Path(sys.executable).parent.as_posix()+'\n',encoding='utf-8')
        text+='LoadModule dav_pgvn_module modules/mod_dav_pgvn.so\nLoadModule authz_pgvn_module modules/mod_authz_pgvn.so\n'
        text+='PgVNConfig "'+native.as_posix()+'"\n<Location />\nDAV pgvn\n'
        text+='PgVNPathAuthz short_circuit\nPgVNAdvertiseV2Protocol On\n'+auth+'</Location>\n'
        executable=APACHE/'bin/pgvnhttpd.exe'
    else:
        modules=area/'control-modules';modules.mkdir(exist_ok=False)
        for name,record in pinned['stock_providers'].items():
            destination=modules/name;shutil.copy2(record['path'],destination)
            assert sha(destination)==record['sha256']
        text+='LoadModule dav_svn_module "'+(modules/'mod_dav_svn.so').as_posix()+'"\n'
        text+='LoadModule authz_svn_module "'+(modules/'mod_authz_svn.so').as_posix()+'"\n'
        authz=area/'svn-test-work/authz';authz.parent.mkdir(parents=True,exist_ok=True)
        authz.write_text('[/]\n* = rw\n',encoding='utf-8')
        # Both Greek/Trojan pristine repositories and ordinary sandboxes are
        # direct children of these two official upstream fixture directories.
        for relative in ('svn-test-work/repositories','svn-test-work/local_tmp'):
            parent=area/relative;parent.mkdir(parents=True,exist_ok=True)
            text+='<Location /'+relative+'>\nDAV svn\nSVNParentPath "'+parent.as_posix()+'"\n'
            text+='SVNAdvertiseV2Protocol On\nAuthzSVNAccessFile "'+(area/'svn-test-work/authz').as_posix()+'"\n'
            text+=auth+'</Location>\n'
        executable=APACHE/'bin/httpd.exe'
    if tls:
        text+='LoadModule socache_shmcb_module modules/mod_socache_shmcb.so\nLoadModule ssl_module modules/mod_ssl.so\n'
        text+='SSLSessionCache none\nSSLEngine On\nSSLProtocol -all +TLSv1.2 +TLSv1.3\n'
        text+='SSLCertificateFile "'+Path(tls['certificate']).as_posix()+'"\n'
        text+='SSLCertificateKeyFile "'+Path(tls['key']).as_posix()+'"\n'
    config=area/'httpd.conf';config.write_text(text,encoding='utf-8')
    # Syntax validation happens under the already guarded M6 database scope;
    # this function does not claim that module configuration is SQL-free.
    run(executable,['-t','-f',config],area,env)
    return {'command':[str(executable),'-X','-f',str(config)],'config':str(config),
            'config_sha256':sha(config),'executable_sha256':sha(executable),
            'scheme':'https' if tls else 'http','tls':tls,'pinned_inputs':pinned}
