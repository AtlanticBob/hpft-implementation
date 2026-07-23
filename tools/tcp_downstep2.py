#!/usr/bin/env python3
"""True TCP shaper DOWN-step data-plane response, using an in-process direct
bpf() map writer (no fork, ~48us/update) so the tool overhead is excluded.
Run as root. Only down-steps are measured (up = TCP cwnd, not the shaper)."""
import json, sys, time, threading
from pathlib import Path
sys.path.insert(0, "/home/zhaoxiang/hyperfront/hpft-exp/tcp_shaper/tools")
from tcp_shaper_lib import build_pair_cfg_update, DirectBpfMapWriter, make_generation

REG = "/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json"
SRC = "sgpu01/0000:38:00.1/vf0"; DST = "sgpu02/0000:38:00.1/vf0"
PIN = Path("/sys/fs/bpf/hpft_tcp_edt")
DEV = "dpu1vf0"; CNT = f"/sys/class/net/{DEV}/statistics/tx_bytes"
HIGH, LOW = 15_000_000_000, 5_000_000_000
LOW_G = LOW/1e9
N = int(sys.argv[1]) if len(sys.argv) > 1 else 25
registry = json.load(open(REG))
writer = DirectBpfMapWriter(PIN).__enter__()

def set_rate(bps):
    u = build_pair_cfg_update(registry=registry, src_vnic=SRC, dst_vnic=DST,
                              rate_bps=bps, burst_bytes=262144, generation=make_generation())
    return writer.update(u)          # ns

samples = []; stop = False
def sampler():
    f = open(CNT)
    while not stop:
        f.seek(0); samples.append((time.monotonic_ns(), int(f.read())))
        time.sleep(0.002)
threading.Thread(target=sampler, daemon=True).start()

def rate(win=15_000_000):
    if len(samples) < 2: return 0.0
    t1,v1 = samples[-1]
    for t0,v0 in reversed(samples):
        if t1-t0 >= win: return (v1-v0)*8/(t1-t0)
    t0,v0 = samples[0]; return (v1-v0)*8/(t1-t0) if t1>t0 else 0.0

lats=[]; upd_ns=[]
for i in range(N):
    set_rate(HIGH); time.sleep(1.0)          # fill to HIGH (not counted)
    r_hi = rate()
    ns = set_rate(LOW); t_cmd = time.monotonic_ns()
    upd_ns.append(ns)
    settle=None; ok=0; dl=t_cmd+500_000_000
    while time.monotonic_ns() < dl:
        if abs(rate()-LOW_G)/LOW_G < 0.12:
            ok+=1
            if ok>=3: settle=time.monotonic_ns(); break
        else: ok=0
        time.sleep(0.002)
    if settle:
        lat=(settle-t_cmd)/1e6; lats.append(lat)
        print(f"rep {i:2d}: hi={r_hi:4.1f}G  down->5G settle={lat:6.1f}ms  (bpf update={ns/1000:.0f}us)", flush=True)
    else:
        print(f"rep {i:2d}: hi={r_hi:4.1f}G  no settle in 500ms", flush=True)
    time.sleep(0.15)
stop=True
if lats:
    lats.sort(); p=lambda q: lats[min(len(lats)-1,int(len(lats)*q))]
    print(f"\nDOWN-step 15G->5G data-plane response  n={len(lats)}  "
          f"min={lats[0]:.1f} p50={p(0.5):.1f} p90={p(0.9):.1f} p99={p(0.99):.1f} max={lats[-1]:.1f} ms")
    print(f"  bpf map update itself: mean {sum(upd_ns)/len(upd_ns)/1000:.0f} us")
