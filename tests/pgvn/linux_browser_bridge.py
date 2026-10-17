"""Opaque stdio/TCP bridge inside the disposable test container."""
import socket
import sys
import threading
sock=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=10)
sock.settimeout(None)
def upload():
    try:
        while True:
            data=sys.stdin.buffer.read1(65536)
            if not data:break
            sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
    except OSError:pass
threading.Thread(target=upload,daemon=True).start()
try:
    while True:
        data=sock.recv(65536)
        if not data:break
        sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
finally:sock.close()
