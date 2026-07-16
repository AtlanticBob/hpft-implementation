#!/usr/bin/env python3
"""Soft-floor collapse safety. vf0 alone[0,30], +vf1/2/3 @30 -> vf0 share
crashes 97->24G. SAFETY: peak aggregate in [30,45] (>~100G = overshot root
= congestion); vf0 give-way time (30 -> within 20% of 24G); steady root
[50,88] util + per-flow sd (synchronized oscillation)."""
import json,sys
from pathlib import Path
DIR=Path(".")
F=lambda n:"sgpu01/vf%d>sgpu02/vf%d|rdma"%(n,n)
def load(tag):
    try: t0=float((DIR/f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError: return None
    per={n:{} for n in range(4)}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=int(rec["ts"]-t0)
        for n in range(4): per[n].setdefault(s,[]).append(rec["r"].get(F(n),0)/1e9)
    return {n:{x:sum(v)/len(v) for x,v in d.items()} for n,d in per.items()}
def rep(tag):
    P=load(tag)
    if P is None: return "no-data"
    xs=sorted(set().union(*[set(P[n]) for n in range(4)]))
    agg={x:sum(P[n].get(x,0) for n in range(4)) for x in xs}
    # crash: peak aggregate [30,45]
    peak=max(agg[x] for x in xs if 30<=x<=45)
    # vf0 give-way: from 30, first x where vf0<=24*1.2
    gw=None
    for x in xs:
        if x>=30 and P[0].get(x,99)<=28.8: gw=x-30; break
    # steady root [50,88]
    st=[x for x in xs if 50<=x<=88]
    aggm=sum(agg[x] for x in st)/len(st)
    fl={n:sum(P[n][x] for x in st if x in P[n])/len([x for x in st if x in P[n]]) for n in range(4)}
    sd=(sum((fl[n]-sum(fl.values())/4)**2 for n in range(4))/4)**.5
    # per-flow temporal sd (oscillation) averaged
    osc=sum((sum((P[n][x]-fl[n])**2 for x in st if x in P[n])/len([x for x in st if x in P[n]]))**.5 for n in range(4))/4
    return dict(peak=peak,gw=gw,aggm=aggm,util=100*aggm/97,sd=sd,osc=osc,fl=fl)
print("Soft-floor CRASH-incast safety (vf0 97G -> 24G @t=30; root~97G)")
print("%-12s | crash: peakAgg giveway | steady: aggUtil flow_sd osc | per-flow")
for cfg,tag in [("zero(0.3)","ci_zero0.3"),("floor 0.5e","ci_floor0.5"),("floor 0.8e","ci_floor0.8")]:
    r=rep(tag)
    if isinstance(r,str): print("%-12s | %s"%(cfg,r)); continue
    gw="%.0fs"%r["gw"] if r["gw"] is not None else ">15s!"
    print("%-12s | %5.0fG   %-5s | %4.0f%%  %.2f  %.2f | %s"
          %(cfg,r["peak"],gw,r["util"],r["sd"],r["osc"],
            " ".join("%.0f"%r["fl"][n] for n in range(4))))
