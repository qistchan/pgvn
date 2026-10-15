"""Disposable loopback TCP proxy with byte accounting and one PG reply fault.

Callers must verify ownership of their isolated database before using it.
The PostgreSQL mode inspects statement text only to identify publication;
it never records parameters, passwords, file data or query text in evidence.
"""
import select
import socket
import struct
import threading
import time


class TcpProxy:
    def __init__(self,target_port,drop_publication_reply=False):
        self.target_port=target_port; self.drop_publication_reply=drop_publication_reply
        self.stop=threading.Event(); self.dropped=threading.Event(); self.lock=threading.Lock()
        self.armed=False; self.received=0; self.sent=0; self.connections=0; self.active=0
        self.sockets=[]; self.workers=[]; self.errors=[]
        self.listener=socket.socket(); self.listener.bind(("127.0.0.1",0)); self.port=self.listener.getsockname()[1]
        self.listener.listen(32); self.listener.settimeout(.2)
        self.thread=threading.Thread(target=self.accept,daemon=True); self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try: client,_=self.listener.accept()
            except socket.timeout: continue
            except OSError: break
            with self.lock: self.active+=1; self.connections+=1
            worker=threading.Thread(target=self.forward,args=(client,),daemon=True)
            self.workers.append(worker); worker.start()

    def statements(self,state,data):
        state["client"].extend(data)
        while True:
            buffer=state["client"]
            if not state["startup"]:
                if len(buffer)<4: return
                length=struct.unpack("!I",buffer[:4])[0]
                if length<8 or length>1048576: raise ValueError("Invalid isolated PG startup length")
                if len(buffer)<length: return
                del buffer[:length]; state["startup"]=True
                continue
            if len(buffer)<5:return
            tag=buffer[0]; length=struct.unpack("!I",buffer[1:5])[0]
            if length<4 or length>16777216:raise ValueError("Invalid isolated PG frame length")
            if len(buffer)<length+1:return
            body=bytes(buffer[5:length+1]);del buffer[:length+1]
            query=body.split(b"\0",2)[1] if tag==ord("P") else body[:-1] if tag==ord("Q") else b""
            query=query.strip().upper()
            if b"UPDATE PGFS.REPOSITORIES SET HEAD_REV" in query: state["publication"]=True
            if state["publication"] and query==b"COMMIT":
                with self.lock:
                    if not self.armed: self.armed=True; state["drop"]=True

    def reply(self,state,data):
        state["server"].extend(data)
        ready=[]
        while len(state["server"])>=5:
            buffer=state["server"];length=struct.unpack("!I",buffer[1:5])[0]
            if length<4 or length>16777216:raise ValueError("Invalid isolated PG response length")
            if len(buffer)<length+1:break
            frame=bytes(buffer[:length+1]);del buffer[:length+1]
            if state["drop"]:
                if frame[:1]==b"C" and frame[5:]==b"COMMIT\0":state["committed"]=True
                if frame[:1]==b"Z":
                    if not state["committed"]:raise AssertionError("Fault did not observe a committed publication")
                    self.dropped.set();return None
            else:ready.append(frame)
        return b"".join(ready)

    def forward(self,client):
        upstream=None
        state={"client":bytearray(),"server":bytearray(),"startup":False,"publication":False,"drop":False,"committed":False}
        try:
            upstream=socket.create_connection(("127.0.0.1",self.target_port),timeout=3)
            client.settimeout(3);upstream.settimeout(3)
            with self.lock:self.sockets.extend((client,upstream))
            while not self.stop.is_set():
                readable,_,_=select.select((client,upstream),(),(),.1)
                for source in readable:
                    data=source.recv(65536)
                    if not data:return
                    with self.lock:
                        if source is client:self.received+=len(data)
                        else:self.sent+=len(data)
                    if self.drop_publication_reply:
                        if source is client:self.statements(state,data)
                        else:
                            data=self.reply(state,data)
                            if data is None:return
                    if data:(upstream if source is client else client).sendall(data)
        except OSError:
            pass
        except Exception as error:
            with self.lock:self.errors.append(type(error).__name__+": "+str(error))
        finally:
            for connection in (client,upstream):
                if connection:
                    try:connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    connection.close()
            with self.lock:self.active-=1

    def idle(self,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.lock:
                if not self.active:return
            time.sleep(.02)
        raise AssertionError("Disposable TCP proxy did not drain")

    def close(self):
        self.stop.set();self.listener.close();self.thread.join(timeout=3)
        with self.lock:
            for connection in self.sockets:
                try:connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                connection.close()
        for worker in self.workers:worker.join(timeout=3)
        assert not self.thread.is_alive() and not any(worker.is_alive() for worker in self.workers)
        assert not self.errors,self.errors
