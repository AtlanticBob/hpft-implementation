#!/usr/bin/env python3
"""Down-step trajectory: capture the raw tx-rate curve after a down-step with
minimal (4ms) smoothing to remove window-induced lag, and report the true time
for the wire rate to cross from HIGH down to the new cap. Run as root."""
import json, sys, time, threading
from pathlib import Path
sys.path.insert(0, "/home/zhaoxiang/hyperfront/hpft-exp/tcp_shaper/tools")
from tcp_shaper_lib import build_pair_cfg_update, DirectBpfMapWriter, make_generation
REG="/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json"
SRC="sgpu01/0000:38:00.1/vf0"; DST="sgpu02/0000:38:00.1/vf0"
PIN=Path("/sys/fs/bpf/hpft_tcp_edt"); DEV="dpu1vf0"; CNT=f"/sys/class/net/{DEV}/statistics/tx_bytes"
HIGH,LOW=15_000_000_000,5_000_000_000; LOW_G=5.0
N=int(sys.argv[1]) if len(sys.argv)>1 else 8
registry=json.load(open(REG)); writer=DirectBpfMapWriter(PIN).__enter__()
def set_rate(bps):
    return writer.update(build_pair_cfg_update(registry=registry,src_vnic=SRC,dst_vnic=DST,
        rate_bps=bps,burst_bytes=262144,generation=make_generation()))
samples=[]; stop=False
def sampler():
    f=open(CNT)
    while not stop:
        f.seek(0); samples.append((time.monotonic_ns(),int(f.read()))); time.sleep(0.001)
threading.Thread(target=sampler,daemon=True).start()

def rate_at(idx, win=4_000_000):
    t1,v1=samples[idx]
    j=idx
    while j>0 and t1-samples[j][0]<win: j-=1
    t0,v0=samples[j]; return (v1-v0)*8/(t1-t0) if t1>t0 else 0.0

cross=[]
for i in range(N):
    set_rate(HIGH); time.sleep(1.0)
    base=len(samples); t_cmd=time.monotonic_ns(); set_rate(LOW)
    time.sleep(0.15)   # capture 150ms of trajectory
    # find first index after t_cmd where 4ms-rate <= 6G (crossed toward new cap) and stays
    seg=[(t,v) for t,v in samples[base:]]
    # recompute rate on the segment
    idxs=range(base+3,len(samples))
    tcross=None; ok=0
    for k in idxs:
        r=rate_at(k)
        if r<=6.0:
            ok+=1
            if ok>=3: tcross=(samples[k][0]-t_cmd)/1e6; break
        else: ok=0
    cross.append(tcross)
    if i==0:
        # print the trajectory of the first step
        print("  first-step trajectory (ms since cmd : Gbps @4ms win):")
        for k in range(base+3,min(base+60,len(samples))):
            dt=(samples[k][0]-t_cmd)/1e6
            if dt<=60: print(f"    {dt:5.1f}ms {rate_at(k):5.1f}G")
    print(f"rep {i}: cross-to-<=6G at {tcross:.1f}ms" if tcross else f"rep {i}: no cross", flush=True)
    time.sleep(0.1)
stop=True
c=[x for x in cross if x is not None]; c.sort()
if c:
    print(f"\nTRUE down-step response (15G->5G, 4ms window, cross to <=6G): "
          f"n={len(c)} min={c[0]:.1f} p50={c[len(c)//2]:.1f} max={c[-1]:.1f} ms")
