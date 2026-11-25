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


LOG='/opt/workspace/tmp/forward.log'


def _note(text):
    try:
        with open(LOG,'a') as f:f.write('%.3f %s\n'%(time.time(),text))
    except OSError:pass


def _serve(path,listener):
    """Never exits: a dead accept thread fails every later model call while the listener still looks open (2026-11-25)."""
    while True:
        client=upstream=None
        try:
            client,_=listener.accept()
            upstream=socket.socket(socket.AF_UNIX)
            upstream.connect(path)
            _pair(client,upstream)
        except BaseException as exc:   # noqa: BLE001 - any error here must not end the forwarder
            _note('forward_error %s %s'%(type(exc).__name__,exc))
            for sock in (client,upstream):
                if sock is not None:
                    try:sock.close()
                    except OSError:pass
            time.sleep(0.2)


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
