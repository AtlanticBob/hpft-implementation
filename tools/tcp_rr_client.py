import socket,sys,time
host,port,n=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
c=socket.socket(); c.connect((host,port)); c.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
lat=[]
for i in range(n):
    t=time.perf_counter_ns(); c.sendall(b"x"); c.recv(16); lat.append((time.perf_counter_ns()-t)/1000)  # us round-trip
c.close(); lat.sort()
p=lambda q: lat[min(len(lat)-1,int(len(lat)*q))]
print(f"TCP_RR n={n}  min={lat[0]:.1f} p50={p(0.5):.1f} p90={p(0.9):.1f} p99={p(0.99):.1f} max={lat[-1]:.1f} us (round-trip)")
