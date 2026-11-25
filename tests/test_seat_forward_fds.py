import os,socket,threading,time
from sniper_office import seat_forward as F


def test_pairs_close_both_sockets():
    a,b=socket.socketpair();c,d=socket.socketpair()
    F._pair(b,c)   # b<->c relayed; a and d are the outer ends
    a.sendall(b'hi');assert d.recv(2)==b'hi'
    a.close();d.close()
    for _ in range(50):
        if b.fileno()==-1 and c.fileno()==-1:break
        time.sleep(0.02)
    assert b.fileno()==-1 and c.fileno()==-1



def test_serve_survives_any_error(monkeypatch,tmp_path):
    monkeypatch.setattr(F,'LOG',str(tmp_path/'f.log'))
    calls=[]
    class L:
        def accept(self):
            calls.append(1)
            if len(calls)<3:raise RuntimeError('boom')
            time.sleep(60)
    threading.Thread(target=F._serve,args=('/nope',L()),daemon=True).start()
    for _ in range(100):
        if len(calls)>=3:break
        time.sleep(0.05)
    assert len(calls)>=3 and 'forward_error RuntimeError boom' in (tmp_path/'f.log').read_text()
