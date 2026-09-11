# TCP request-response probe: n ping-pongs of sz bytes over one connection.
#   python3 tcp_rr_client.py <host> <port> <n> [sz] [dump]
# prints "RR sz=.. n=.. p50=.. p90=.. p99=.. us"; with dump, also writes every
# sample (us, sorted) one per line to that file. Pairs with tcp_rr_server.py.
# The timer is Python's perf_counter_ns around sendall+recv, so the floor
# includes tens of microseconds of interpreter on each side.
import socket, sys, time
host, port, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
sz = int(sys.argv[4]) if len(sys.argv) > 4 else 1
dump = sys.argv[5] if len(sys.argv) > 5 else None
c = socket.socket(); c.connect((host, port)); c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
payload = b"x" * sz; lat = []
for i in range(n):
    t = time.perf_counter_ns(); c.sendall(payload)
    got = 0
    while got < sz:
        d = c.recv(sz - got)
        if not d:
            break
        got += len(d)
    lat.append((time.perf_counter_ns() - t) / 1000)
c.close(); lat.sort()
p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))]
print(f"RR sz={sz} n={n} p50={p(0.5):.1f} p90={p(0.9):.1f} p99={p(0.99):.1f} p999={p(0.999):.1f} us")
if dump:
    with open(dump, "w") as f:
        f.write("\n".join("%.2f" % x for x in lat) + "\n")
