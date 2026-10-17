"""Real PG sessions through a blackholed TCP proxy and raw monitor clients."""
from pathlib import Path
import json
import concurrent.futures
import socket
import subprocess
import threading
import time


def database_deadlines(root, product, sdk, work, env):
    binary=work/'pq-deadline-probe'
    args=['cc','-std=c17','-D_POSIX_C_SOURCE=200809L','-Wall','-Wextra','-Werror',
          '-I'+str(root/'src/libsvn_fs_pg'),'-I'+str(sdk/'include'),root/'tests/pgvn/pq_deadline_probe.c',
          '-L'+str(product/'lib'),'-Wl,-rpath,'+str(product/'lib'),'-lpq','-o',binary]
    if 'sanitized' in product.name:args[1:1]=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
    subprocess.run(list(map(str,args)),check=True,env=env,capture_output=True)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(8);listener.settimeout(.1)
    blocked=threading.Event();stopping=threading.Event();sockets=[];threads=[]
    def relay(source,target):
        try:
            while not stopping.is_set():
                data=source.recv(65536)
                if not data:break
                if not blocked.is_set():target.sendall(data)
        except OSError:pass
    def accept():
        while not stopping.is_set():
            try:client,_=listener.accept()
            except socket.timeout:continue
            except OSError:break
            server=socket.create_connection(('127.0.0.1',5432));sockets.extend((client,server))
            for source,target in ((client,server),(server,client)):
                thread=threading.Thread(target=relay,args=(source,target),daemon=True);threads.append(thread);thread.start()
    accept_thread=threading.Thread(target=accept,daemon=True);accept_thread.start();results=[]
    try:
        for mode in ('timeout','cancel'):
            blocked.clear()
            dsn='host=127.0.0.1 port='+str(listener.getsockname()[1])+' dbname=pgvn_linux user=linux_owner sslmode=disable connect_timeout=3'
            process=subprocess.Popen([binary,dsn,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            try:
                assert process.stdout.readline().strip()=='READY'
                blocked.set();output,error=process.communicate('\n',timeout=5)
                assert process.returncode==0,(output,error)
                results.append({'mode':mode,**json.loads(output)})
            finally:
                if process.poll() is None:process.kill();process.wait()
        (work/'database-deadlines.json').write_text(json.dumps(results,indent=2)+'\n')
    finally:
        stopping.set();listener.close()
        for sock in sockets:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            sock.close()
        accept_thread.join(timeout=1)
        for thread in threads:thread.join(timeout=1)
    return results


def monitor_limits(port):
    def exchange(request):
        with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
            sock.sendall(request);data=b''
            while True:
                try:chunk=sock.recv(65536)
                except ConnectionResetError:return 0
                if not chunk:break
                data+=chunk
            return int(data.split(b' ',2)[1]) if data else 0
    host=b'Host: 127.0.0.1:'+str(port).encode()+b'\r\nConnection: close\r\n'
    assert exchange(b'POST /health/live HTTP/1.1\r\n'+host+b'Content-Length: 0\r\n\r\n')==405
    assert exchange(b'GET /health/live HTTP/1.1\r\n'+host+b'Content-Length: 2\r\n\r\nxx')==413
    assert exchange(b'GET /health/live HTTP/1.1\r\n'+host+b'X-Large: '+b'x'*40000+b'\r\n\r\n') in (0,400,413,431)
    sockets=[];start=time.monotonic()
    try:
        for _ in range(64):
            sock=socket.create_connection(('127.0.0.1',port),timeout=2);sockets.append(sock)
            sock.sendall(b'GET /health/live HTTP/1.1\r\nHost: ')
        for sock in sockets:
            sock.settimeout(7);assert sock.recv(1)==b''
        elapsed=time.monotonic()-start
        assert 4<=elapsed<8,elapsed
    finally:
        for sock in sockets:sock.close()
    assert exchange(b'GET /health/live HTTP/1.1\r\n'+host+b'\r\n')==200
    for _ in range(32):
        with socket.create_connection(('127.0.0.1',port),timeout=2) as sock:
            sock.sendall(b'GET /health/live HTTP/1.1\r\n'+host+b'\r\n')
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        statuses=list(pool.map(lambda _:exchange(b'GET /health/live HTTP/1.1\r\n'+host+b'\r\n'),range(64)))
    assert statuses==[200]*64,statuses
    return {'queued_requests':64,'slow_headers':64,'deadline_seconds':elapsed,'recovered':True}
