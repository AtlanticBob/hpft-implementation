import json,sys,time,threading
from pathlib import Path
sys.path.insert(0,"/home/zhaoxiang/hyperfront/hpft-exp/tcp_shaper/tools")
from tcp_shaper_lib import build_pair_cfg_update, DirectBpfMapWriter, make_generation
REG="/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json"
SRC="sgpu01/0000:38:00.1/vf0"; DST="sgpu02/0000:38:00.1/vf0"
PIN=Path("/sys/fs/bpf/hpft_tcp_edt"); CNT="/sys/class/net/dpu1vf0/statistics/tx_bytes"
reg=json.load(open(REG)); w=DirectBpfMapWriter(PIN).__enter__()
def sr(b): return w.update(build_pair_cfg_update(registry=reg,src_vnic=SRC,dst_vnic=DST,rate_bps=b,burst_bytes=262144,generation=make_generation()))
samples=[]; stop=False
def s():
    f=open(CNT)
    while not stop: f.seek(0); samples.append((time.monotonic_ns(),int(f.read()))); time.sleep(0.001)
threading.Thread(target=s,daemon=True).start()
def rate(idx,win=4_000_000):
    t1,v1=samples[idx]; j=idx
    while j>0 and t1-samples[j][0]<win: j-=1
    t0,v0=samples[j]; return (v1-v0)*8/(t1-t0) if t1>t0 else 0
sr(15_000_000_000); time.sleep(1.0)
steps=[15,13,11,9,7,5,3]; DWELL=0.030
marks=[]
for g in steps:
    sr(g*1_000_000_000); marks.append((g,time.monotonic_ns())); time.sleep(DWELL)
time.sleep(0.05); stop=True
# for each step, measured rate at end of its dwell (last 8ms avg)
print("staircase down (in-process direct bpf, 30ms dwell/step):")
for k,(g,tc) in enumerate(marks):
    tend = marks[k+1][1] if k+1<len(marks) else tc+int(DWELL*1e9)
    seg=[rate(i) for i in range(len(samples)) if tend-8_000_000 <= samples[i][0] <= tend]
    avg=sum(seg)/len(seg) if seg else 0
    print(f"  cap={g:2d}G -> measured {avg:4.1f}G  (err {100*(avg-g)/g:+.0f}%)")
