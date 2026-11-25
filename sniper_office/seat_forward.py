"""Runs inside a seat sandbox, stdlib only: 127.0.0.1:3128 to the host proxy's Unix socket. There is no other way out of the namespace."""
import os
import socket
import subprocess
import sys
import threading
import time

PORT=3128


def _copy(src,dst,done):
    try:
        while True:
            data=src.recv(65536)
            if not data:break
            dst.sendall(data)
    except OSError:pass   # either side closing ends the tunnel; the host proxy logs the outcome
    finally:
        try:dst.shutdown(socket.SHUT_WR)
        except OSError:pass
        done()


def _pair(client,upstream):
    """Both sockets close once both directions end: an unclosed pair leaks two fds, and at the fd limit accept() fails for good (2026-11-25 seat hangs)."""
    left=[2];lock=threading.Lock()
    def done():
        with lock:
            left[0]-=1
            if left[0]:return
        client.close();upstream.close()
    for a,b in ((client,upstream),(upstream,client)):threading.Thread(target=_copy,args=(a,b,done),daemon=True).start()


def _serve(path,listener):
    while True:
        try:client,_=listener.accept()
        except OSError:
            time.sleep(0.2);continue   # e.g. EMFILE: wait for fds to free up, never let the forwarder die
        upstream=socket.socket(socket.AF_UNIX)
        try:upstream.connect(path)
        except OSError:
            client.close();upstream.close();continue
        _pair(client,upstream)


def start(path='/opt/proxy.sock'):
    listener=socket.socket()
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    listener.bind(('127.0.0.1',PORT));listener.listen(64)
    threading.Thread(target=_serve,args=(path,listener),daemon=True).start()
    url='http://127.0.0.1:%d'%PORT
    return dict(HTTPS_PROXY=url,https_proxy=url,HTTP_PROXY=url,http_proxy=url,NO_PROXY='',no_proxy='')


if __name__=='__main__':
    # Tool mode: run one command with the proxy up, then leave.
    env=dict(os.environ,**start())
    sys.exit(subprocess.run(sys.argv[1:],env=env).returncode)
