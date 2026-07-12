#!/usr/bin/env python3
"""Discard sink for the adversarial pump (runs on the receiver host)."""
import socket
import sys

port = int(sys.argv[1])
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", port))
srv.listen(4)
print("sink: listening :%d" % port, flush=True)
while True:
    c, addr = srv.accept()
    print("sink: accept %s" % (addr,), flush=True)
    buf = bytearray(1 << 20)
    try:
        while c.recv_into(buf):
            pass
    except OSError:
        pass
    c.close()
