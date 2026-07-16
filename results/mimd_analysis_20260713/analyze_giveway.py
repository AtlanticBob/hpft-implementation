#!/usr/bin/env python3
"""Give-way down-step undershoot, replicates. Per run: validate TCP up in
[15,25]; steady RDMA [35,55]; dip = min RDMA [15,25]; undershoot% =
(steady-dip)/steady. Median over valid reps. mimd vs aimd."""
import json,sys
from pathlib import Path
DIR=Path(sys.argv[1] if len(sys.argv)>1 else ".")
FR="sgpu01/vf0>sgpu02/vf0|rdma"; FT="sgpu01/vf0>sgpu02/vf0|tcp"
def one(tag):
    try: t0=float((DIR/f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError: return None
    rr={};tt={}
    for jl in (DIR/f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec=json.loads(jl)
        except: continue
        s=rec["ts"]-t0
        rr.setdefault(int(s),[]).append(rec["r"].get(FR,0)/1e9)
        tt.setdefault(int(s),[]).append(rec["r"].get(FT,0)/1e9)
    R={x:sum(v)/len(v) for x,v in rr.items()}; T={x:sum(v)/len(v) for x,v in tt.items()}
    tcp_win=[T[x] for x in range(15,26) if x in T]
    if not tcp_win or sum(tcp_win)/len(tcp_win)<3.0: return ("badTCP",)  # TCP didn't compete
    steady=[R[x] for x in range(35,56) if x in R]; sv=sum(steady)/len(steady)
    dip=min(R[x] for x in range(15,26) if x in R)
    return (sv, dip, (sv-dip)/sv*100 if sv>0 else 0)
def med(xs): xs=sorted(xs); n=len(xs); return xs[n//2] if n%2 else (xs[n//2-1]+xs[n//2])/2
print("give-way down-step undershoot (RDMA vf0 @20G, TCP joins @15)")
for law in ("mimd","aimd"):
    us=[]
    for rep in (1,2,3):
        r=one(f"gw_{law}_r{rep}")
        if r is None: tag_s="no-data"
        elif r[0]=="badTCP": tag_s="TCP<3G discard"
        else: tag_s="steady=%.1f dip=%.1f under=%.1f%%"%r; us.append(r[2])
        print("  %-5s r%d: %s"%(law,rep,tag_s))
    if us: print("  %-5s  MEDIAN undershoot = %.1f%%  (n=%d valid)"%(law,med(us),len(us)))
    print()
