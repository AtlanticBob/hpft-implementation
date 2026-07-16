#!/usr/bin/env python3
"""Exp 2.2+2.3 step response / scale-invariance. RDMA vf0, 2s-smooth.
DOWN step @15 (TCP joins, RDMA C->~C/2); UP step @45 (TCP leaves, RDMA
->C). Per step: settling to within 10% of new steady + over/undershoot%.
Scale-invariance = UP settling vs cap (MIMD flat, AIMD grows)."""
import json,sys
from pathlib import Path
DIR=Path(sys.argv[1] if len(sys.argv)>1 else ".")
FR="sgpu01/vf0>sgpu02/vf0|rdma"
def load(tag):
    t0=float((DIR/f"{tag}_t0.txt").read_text().strip()); ser={}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=int(rec["ts"]-t0); ser.setdefault(s,[]).append(rec["r"].get(FR,0)/1e9)
    xs=sorted(ser); raw={x:sum(ser[x])/len(ser[x]) for x in xs}
    return {x:sum(raw[k] for k in xs if x-2<k<=x)/len([k for k in xs if x-2<k<=x]) for x in xs}
def step(sm, t_ev, steady_lo, steady_hi, direction):
    final=[v for x,v in sm.items() if steady_lo<=x<=steady_hi]
    if not final: return None
    fv=sum(final)/len(final); band=0.10*fv; last=t_ev
    over=0.0
    for x,v in sorted(sm.items()):
        if t_ev<=x<=steady_hi:
            if direction=="up": over=max(over,(v-fv)/fv)     # overshoot above final
            else: over=max(over,(fv-v)/fv)                    # undershoot below final
            if abs(v-fv)>band: last=x
    return fv, last-t_ev, over*100
print("Exp2.2/2.3 step response  (RDMA vf0)")
print("%-5s %4s | DOWN@15 final settle under%% | UP@45 final settle over%%"%("law","cap"))
res={}
for law in ("mimd","aimd"):
    for cap in (2,6,20,40):
        tag=f"st_{law}_c{cap}"
        try: sm=load(tag)
        except FileNotFoundError: print("%-5s %4d | (no data)"%(law,cap)); continue
        d=step(sm,15,35,44,"down"); u=step(sm,45,80,89,"up")
        if d and u:
            res[(law,cap)]=(d,u)
            print("%-5s %4d | %5.2f  %5.1fs  %4.1f      | %5.2f  %5.1fs  %4.1f"
                  %(law,cap,d[0],d[1],d[2],u[0],u[1],u[2]))
print()
print("SCALE-INVARIANCE (UP-step settling vs cap):")
for law in ("mimd","aimd"):
    row=[(cap,res[(law,cap)][1][1]) for cap in (2,6,20,40) if (law,cap) in res]
    print("  %-5s "%law+"  ".join("c%d=%.1fs"%(c,s) for c,s in row))
