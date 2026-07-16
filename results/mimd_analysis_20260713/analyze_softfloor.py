#!/usr/bin/env python3
"""Soft-floor MD give-way. Reuse give-way metric. Groups: zero rebaseline,
floor@{0.3,0.5,0.8}e_hat. Median undershoot vs baseline (mimd 47.1/aimd 43.8)."""
import json,sys
from pathlib import Path
DIR=Path(".")
FR="sgpu01/vf0>sgpu02/vf0|rdma"; FT="sgpu01/vf0>sgpu02/vf0|tcp"
def one(tag):
    try: t0=float((DIR/f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError: return None
    rr={};tt={}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=rec["ts"]-t0
        rr.setdefault(int(s),[]).append(rec["r"].get(FR,0)/1e9); tt.setdefault(int(s),[]).append(rec["r"].get(FT,0)/1e9)
    R={x:sum(v)/len(v) for x,v in rr.items()}; T={x:sum(v)/len(v) for x,v in tt.items()}
    tw=[T[x] for x in range(15,26) if x in T]
    if not tw or sum(tw)/len(tw)<3.0: return "badTCP"
    steady=[R[x] for x in range(35,56) if x in R]; sv=sum(steady)/len(steady)
    dip=min(R[x] for x in range(15,26) if x in R)
    return (sv,dip,(sv-dip)/sv*100 if sv>0 else 0)
def med(xs): xs=sorted(xs); n=len(xs); return xs[n//2] if n%2 else (xs[n//2-1]+xs[n//2])/2
print("Soft-floor MD give-way (RDMA vf0 @20G; baseline: mimd-zero 47.1%, aimd 43.8%)")
groups=[("zero(rebaseline)",["gw2_zero_r1"]),
        ("floor 0.3e",["gw2_f0.3_r1","gw2_f0.3_r2","gw2_f0.3_r3"]),
        ("floor 0.5e",["gw2_f0.5_r1","gw2_f0.5_r2","gw2_f0.5_r3"]),
        ("floor 0.8e",["gw2_f0.8_r1","gw2_f0.8_r2","gw2_f0.8_r3"])]
for name,tags in groups:
    us=[];det=[]
    for tg in tags:
        r=one(tg)
        if r is None: det.append("no-data")
        elif r=="badTCP": det.append("TCPbad")
        else: det.append("under=%.1f%%(dip%.1f/std%.1f)"%(r[2],r[1],r[0])); us.append(r[2])
    m="  MEDIAN=%.1f%%"%med(us) if us else "  (no valid)"
    print("  %-16s %s %s"%(name,", ".join(det),m))
