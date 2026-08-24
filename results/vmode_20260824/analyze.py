#!/usr/bin/env python3
"""V 两条臂的对照分析。

刺激是运行中把目的 VM 的额度一步压低：流仍按旧份额在发，新份额更低，于是
r>e，账本在这段窗口里真正工作（稳态下 r<=e、s 恒为 0，两条臂无从区分）。

阶跃时刻**从数据里检出**（ê 的跌落），不用外部时戳——推送 registry 到四台
DPU 要几秒，外部时戳量到的是推送延迟而不是系统的反应。

每条流集合报四个量：
  V_eff = vq/s   实测的满刻度。它同时是**臂的自检**：global 臂应当是一个常数，
                 per_fs 臂应当正比于该流集合的 ê。
  s_peak         标记涨到多高（跟踪律把 r 拉回 ê 之前）
  t_peak         涨到峰值用了多久
  ds/dt          阶跃后头几拍的充入速率 = 超额/V
"""
import json, sys
from pathlib import Path

DIR = Path(__file__).resolve().parent / "results"


def per_fs(tag):
    rows = {}
    for line in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for f in (rec.get("r") or {}):
            if not f.endswith("rdma"):
                continue
            rows.setdefault(f, []).append((
                rec["ts"], rec["r"][f], rec.get("e", {}).get(f, 0),
                rec.get("c", {}).get(f, 0), rec.get("s", {}).get(f, 0),
                rec.get("vq", {}).get(f, 0)))
    for f in rows:
        rows[f].sort()
    return rows


def analyse(tag):
    try:
        rows = per_fs(tag)
    except FileNotFoundError:
        print(f"  {tag}: 缺数据")
        return None
    if not rows:
        print(f"  {tag}: 没有 rdma 流集合")
        return None
    # 阶跃时刻全局定一次：政策一步压低会让**同一拍**里多数流集合的 ê 一起跌。
    # 逐流各自检会把流的起停也当成阶跃——那是另一回事。
    votes = {}
    for f, seq in rows.items():
        for i in range(1, len(seq)):
            if seq[i][3] < 0.7 * seq[i - 1][3] and seq[i - 1][3] > 1e9:
                votes.setdefault(round(seq[i][0], 1), []).append(f)
    if not votes:
        print(f"  {tag}: 没检出额度阶跃")
        return None
    tstep = max(votes, key=lambda t: len(votes[t]))
    nlive = len(votes[tstep])
    out = []
    for f, seq in sorted(rows.items()):
        step = next((i for i in range(len(seq)) if seq[i][0] >= tstep), None)
        if step is None or step >= len(seq) - 3:
            continue
        t0 = seq[step][0]
        win = [x for x in seq[step:] if x[0] - t0 <= 2.0]
        # V_eff：取 s 有意义的样本（避开 0/0）
        vs = [(vq / s) for _, _, _, _, s, vq in win if s > 0.05 and vq > 0]
        veff = sorted(vs)[len(vs) // 2] if vs else float("nan")
        speak = max((s for _, _, _, _, s, _ in win), default=0.0)
        tpk = next((x[0] - t0 for x in win if x[4] >= speak - 1e-9), float("nan"))
        ehat = seq[min(step + 1, len(seq) - 1)][3]   # 阶跃**之后**的 ê
        # 头两拍的充入斜率
        slope = float("nan")
        if len(win) > 2 and win[2][0] > t0:
            slope = (win[2][4] - win[0][4]) / (win[2][0] - t0)
        out.append(dict(fs=f, ehat=ehat / 1e9, veff=veff / 1e6, s_peak=speak,
                        t_peak=tpk * 1000, slope=slope))
    print(f"  {tag}:  (阶跃时同时在跑的流集合 {nlive} 个)")
    for d in out:
        print("    %-32s ê=%5.2fG  V_eff=%7.0f Mb  s_peak=%.2f  t_peak=%4.0f ms  ds/dt=%5.2f /s"
              % (d["fs"], d["ehat"], d["veff"], d["s_peak"], d["t_peak"], d["slope"]))
    return out


if __name__ == "__main__":
    tags = sys.argv[1:] or ["global_sparse", "global_crowded",
                            "per_fs_sparse", "per_fs_crowded"]
    print("=== 额度阶跃后的审计反应 ===")
    got = {t: analyse(t) for t in tags}
    print("\n=== 臂自检：V_eff 应当 global 恒定 / per_fs 正比于 ê ===")
    for arm in ("global", "per_fs"):
        pts = [(d["ehat"], d["veff"]) for t in tags if t.startswith(arm)
               for d in (got.get(t) or [])]
        if len(pts) >= 2:
            for e, v in sorted(pts):
                print("    %-7s ê=%5.2fG -> V_eff=%7.0f Mb   (V/ê = %5.1f ms)"
                      % (arm, e, v, v / e))
    print("\n=== 场景间对比（理论：global 应随 ê 变，per_fs 不应）===")
    for arm in ("global", "per_fs"):
        a, b = got.get(f"{arm}_sparse"), got.get(f"{arm}_crowded")
        if not a or not b:
            continue
        sa = sorted(d["slope"] for d in a)[len(a) // 2]
        sb = sorted(d["slope"] for d in b)[len(b) // 2]
        print("  %-7s 充入速率 sparse %.2f /s vs crowded %.2f /s -> 相差 %.1f 倍"
              % (arm, sa, sb, max(sa, sb) / max(min(sa, sb), 1e-9)))
