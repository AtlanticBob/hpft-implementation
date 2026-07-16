#!/usr/bin/env python3
"""Exp 2.1 gain-margin sweep. For each (alpha,beta,tag): steady RDMA
mean/sd during competition [24,44] (fair~3G), ratio, dip episodes
(<1.5G run>=2s). Oscillation sd grows near the Nyquist margin."""
import json,sys
from pathlib import Path
DIR=Path(sys.argv[1] if len(sys.argv)>1 else ".")
FR="sgpu01/vf0>sgpu02/vf0|rdma"; FT="sgpu01/vf0>sgpu02/vf0|tcp"
def metrics(tag):
    try: t0=float((DIR/f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError: return None
    sec={}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=int(rec["ts"]-t0)
        if 24<=s<44:
            sec.setdefault(s,{"r":[],"t":[]})
            sec[s]["r"].append(rec["r"].get(FR,0)/1e9); sec[s]["t"].append(rec["r"].get(FT,0)/1e9)
    if not sec: return None
    rs=[sum(d["r"])/len(d["r"]) for _,d in sorted(sec.items())]
    ts=[sum(d["t"])/len(d["t"]) for _,d in sorted(sec.items())]
    mr=sum(rs)/len(rs); mt=sum(ts)/len(ts)
    sd=(sum((x-mr)**2 for x in rs)/len(rs))**.5
    ratio=mr/mt if mt>.1 else 0
    eps,run=0,0
    for x in rs:
        run=run+1 if x<1.5 else 0
        if run==2: eps+=1
    return mr,mt,sd,ratio,eps
print("%-6s %-6s  rdmaG  tcpG  ratio   sd     eps  flag"%("alpha","beta"))
for a,b,tag in [(x.split("_")[1][1:],x.split("_")[2][1:],x) for x in sys.argv[2:]]:
    m=metrics(tag)
    if m is None: print("%-6s %-6s  (no data)"%(a,b)); continue
    mr,mt,sd,ratio,eps=m
    flags=[]
    if sd>0.5: flags.append("OSC")
    if abs(ratio-1)>0.15: flags.append("RATIO")
    if eps: flags.append("EPS")
    print("%-6s %-6s  %5.2f %5.2f  %.2f   %5.2f  %3d  %s"%(a,b,mr,mt,ratio,sd,eps,",".join(flags) or "ok"))
