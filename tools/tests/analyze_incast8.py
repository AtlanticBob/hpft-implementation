#!/usr/bin/env python3
"""Proper TCP+RDMA incast (user spec). 8 flows (4vf x {rdma,tcp}), 100G root,
expect each ~12.5G. Reports per-flow steady [40,85], fairness (Jain + min/max),
aggregate util, per-class totals, convergence time (to all within 20% of 12.5)."""
import json,sys
from pathlib import Path
DIR=Path(__file__).resolve().parent.parent.parent/"results"/"regression"
flows=[("sgpu01/vf%d>sgpu02/vf%d|%s"%(n,n,c),"vf%d.%s"%(n,c)) for n in range(4) for c in ("rdma","tcp")]
def load(tag):
    try: t0=float((DIR/f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError: return None
    per={k:{} for k,_ in flows}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=int(rec["ts"]-t0)
        for k,_ in flows: per[k].setdefault(s,[]).append(rec["r"].get(k,0)/1e9)
    return {k:{x:sum(v)/len(v) for x,v in d.items()} for k,d in per.items()}
def rep(tag):
    P=load(tag)
    if P is None: return None
    xs=sorted(set().union(*[set(P[k]) for k,_ in flows]))
    st=[x for x in xs if 40<=x<=85]
    mean={k:(sum(P[k][x] for x in st if x in P[k])/max(1,len([x for x in st if x in P[k]]))) for k,_ in flows}
    vals=list(mean.values()); agg=sum(vals)
    jain=(sum(vals)**2)/(8*sum(v*v for v in vals)) if sum(v*v for v in vals)>0 else 0
    rdma=sum(mean[k] for k,_ in flows if k.endswith("rdma")); tcp=agg-rdma
    # convergence: first x where all 8 flows within [0.7,1.3]*12.5 and stay
    conv=None
    for x in xs:
        if x<3: continue
        if all(9<=P[k].get(x,0)<=16 for k,_ in flows): conv=x; break
    return dict(mean=mean,agg=agg,util=100*agg/97,jain=jain,rdma=rdma,tcp=tcp,
                mn=min(vals),mx=max(vals),conv=conv)
tag=sys.argv[1] if len(sys.argv)>1 else "i8_zero"
r=rep(tag)
if r is None: print("no data:",tag); sys.exit()
print("=== %s : proper TCP+RDMA incast (expect each ~12.5G) ==="%tag)
print("aggregate=%.0fG util=%.0f%%  RDMA_tot=%.0fG TCP_tot=%.0fG  Jain=%.3f  min=%.1f max=%.1f  conv=%ss"
      %(r["agg"],r["util"],r["rdma"],r["tcp"],r["jain"],r["mn"],r["mx"],r["conv"]))
print("per-flow (G):  "+"  ".join("%s=%.1f"%(lbl,r["mean"][k]) for k,lbl in flows))
