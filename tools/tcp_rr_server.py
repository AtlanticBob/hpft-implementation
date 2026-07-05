import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("0.0.0.0",int(sys.argv[1]))); s.listen(8)
while True:
    c,_=s.accept(); c.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    try:
        while True:
            d=c.recv(16)
            if not d: break
            c.sendall(d)
    except Exception: pass
    c.close()
