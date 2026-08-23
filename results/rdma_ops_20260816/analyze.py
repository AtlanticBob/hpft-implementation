#!/usr/bin/env python3
"""incast8 analysis + plot for one run tag in ./results.

C_ROOT is the capacity the receiver's allocator schedules against: the
100G downlink less the registry headroom of 0.08 (3% queue warning + the
~5% VxLAN encapsulation tax, since the ledger counts inner bytes and the
wire carries outer ones). Eight equally weighted flow-sets therefore have
an 11.5 Gb/s share each, which is what the sender's law is seen tracking.
Steady window [40,85] s. Prints per-flow mean, Jain, aggregate, convergence.
Plot: per-flow rate time series (TCP soft orange, RDMA deep blue; one line
style per tenant) + a bar of steady means. Colours from paper/PLOT_STYLE.md."""
import json, sys
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

DIR = Path(__file__).resolve().parent / "results"
TCP, RDMA, FAIR, GRAY = "#EDB877", "#3D5A80", "#2a9d5c", "#888888"
LS = ["-", "--", "-.", ":"]
C_ROOT = 92.0        # 100G downlink x (1 - headroom 0.08)
flows = [("sgpu01/vf%d>sgpu02/vf%d|%s" % (n, n, c), "vf%d.%s" % (n, c), n, c)
         for n in range(4) for c in ("rdma", "tcp")]

def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {k: {} for k, *_ in flows}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec = json.loads(jl)
        except Exception: continue
        s = int(rec["ts"] - t0)
        for k, *_ in flows:
            per[k].setdefault(s, []).append(rec["r"].get(k, 0) / 1e9)
    return {k: {x: sum(v) / len(v) for x, v in d.items()} for k, d in per.items()}

def rep(tag):
    P = load(tag)
    xs = sorted(set().union(*[set(P[k]) for k, *_ in flows]))
    st = [x for x in xs if 40 <= x <= 85]
    mean = {k: sum(P[k].get(x, 0) for x in st) / max(1, len(st)) for k, *_ in flows}
    vals = list(mean.values()); agg = sum(vals)
    jain = agg ** 2 / (8 * sum(v * v for v in vals)) if agg > 0 else 0
    rdma = sum(mean[k] for k, *_ in flows if k.endswith("rdma"))
    conv = None
    for x in xs:
        if x < 3: continue
        if all(9 <= P[k].get(x, 0) <= 16 for k, *_ in flows): conv = x; break
    # stall episodes: seconds in [5, 84] where any RDMA flow-set is below 3G
    # (the cc_rate/qp_count limit cycle of the RDMA execution plane); the
    # clean window is the steady window minus those seconds.
    run = [x for x in xs if 5 <= x <= 84]
    stall = [x for x in run if any(P[k].get(x, 0) < 3 for k, *_ in flows if k.endswith("rdma"))]
    clean = [x for x in st if x not in stall]
    cmean = {k: sum(P[k].get(x, 0) for x in clean) / max(1, len(clean)) for k, *_ in flows}
    cv = list(cmean.values()); cagg = sum(cv)
    cjain = cagg ** 2 / (8 * sum(v * v for v in cv)) if cagg > 0 else 0
    return P, dict(mean=mean, agg=agg, util=100 * agg / C_ROOT, jain=jain, rdma=rdma,
                   tcp=agg - rdma, mn=min(vals), mx=max(vals), conv=conv,
                   stall_s=len(stall), stall_at=stall, cmean=cmean, cagg=cagg, cjain=cjain,
                   cmn=min(cv), cmx=max(cv), clean_n=len(clean))

def plot(tag, P, r, title):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [2.4, 1]})
    for k, lbl, n, c in flows:
        xs = sorted(P[k]); a1.plot(xs, [P[k][x] for x in xs], color=RDMA if c == "rdma" else TCP,
                                    ls=LS[n], lw=1.2, label=lbl)
    a1.axhline(C_ROOT / 8, color=FAIR, ls="--", lw=0.8); a1.text(1, C_ROOT / 8 + 0.4, "policy share %.1fG" % (C_ROOT / 8), color=FAIR, fontsize=7)
    a1.axvspan(40, 85, color="#f2f2f2", zorder=0)
    a1.set_xlabel("time (s)"); a1.set_ylabel("received rate (Gb/s)"); a1.set_title(title, fontsize=10)
    a1.legend(ncol=4, fontsize=7, frameon=False)
    for ax in (a1, a2):
        ax.grid(axis="y", color="#e0e0e0", lw=0.5, zorder=0); ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    xs = list(range(8)); vals = [r["mean"][k] for k, *_ in flows]
    a2.bar(xs, vals, color=[RDMA if c == "rdma" else TCP for *_, c in flows], edgecolor="black", lw=0.4, zorder=3)
    a2.axhline(C_ROOT / 8, color=FAIR, ls="--", lw=0.8)
    a2.set_xticks(xs); a2.set_xticklabels([l for _, l, *_ in flows], rotation=60, fontsize=7)
    a2.set_ylabel("steady mean (Gb/s)"); a2.set_title("Jain=%.3f agg=%.0fG" % (r["jain"], r["agg"]), fontsize=10)
    a2.legend(handles=[Patch(color=RDMA, ec="black", lw=0.4, label="RDMA"), Patch(color=TCP, ec="black", lw=0.4, label="TCP")], fontsize=7, frameon=False)
    fig.tight_layout(); fig.savefig(DIR.parent / f"fig_{tag}.png", dpi=200); fig.savefig(DIR.parent / f"fig_{tag}.pdf")

if __name__ == "__main__":
    tag = sys.argv[1]; title = sys.argv[2] if len(sys.argv) > 2 else tag
    P, r = rep(tag)
    print("=== %s ===" % tag)
    print("aggregate=%.1fG util=%.0f%%  RDMA_tot=%.1fG TCP_tot=%.1fG  Jain=%.3f  min=%.1f max=%.1f  conv=%ss"
          % (r["agg"], r["util"], r["rdma"], r["tcp"], r["jain"], r["mn"], r["mx"], r["conv"]))
    print("per-flow (G):  " + "  ".join("%s=%.1f" % (lbl, r["mean"][k]) for k, lbl, *_ in flows))
    print("stall seconds (any RDMA <3G, t in [5,84]): %d  at %s" % (r["stall_s"], r["stall_at"]))
    print("clean window (%d s): aggregate=%.1fG Jain=%.3f min=%.1f max=%.1f" % (r["clean_n"], r["cagg"], r["cjain"], r["cmn"], r["cmx"]))
    print("clean per-flow (G): " + "  ".join("%s=%.1f" % (lbl, r["cmean"][k]) for k, lbl, *_ in flows))
    plot(tag, P, r, title)
