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
