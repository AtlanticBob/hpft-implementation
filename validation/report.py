#!/usr/bin/env python3
"""reports/<tag>.md from data/ and the run's environment snapshots.
Sections follow README §五: what was run (environment as it actually was, the
arm under test), the flow table, expected vs measured per phase, application
goodput, convergence per event, the six verdicts, mechanism evidence (the
executor's account, CNP and switch counters), figures. usage: report.py <tag>"""
import csv, json, os, re, subprocess, sys
BASE = os.path.dirname(os.path.abspath(__file__))
tag = sys.argv[1]
R = os.path.join(BASE, "results", tag); D = os.path.join(BASE, "data"); O = os.path.join(BASE, "reports")
os.makedirs(O, exist_ok=True)


def rd(name):
    p = os.path.join(D, f"{tag}_{name}.csv")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def txt(name):
    p = os.path.join(R, name)
    return open(p).read().strip() if os.path.exists(p) else ""


reg = json.load(open(os.path.join(R, "registry.json")))
ep = reg["e_params"]
env = txt("env_status.txt")
env_lines = [l for l in env.splitlines() if re.search(r"UPCC|ECN profile|environment:|^HOST|^sgpu0[1-4]\s", l)]
flows_tbl = subprocess.check_output([sys.executable, os.path.join(BASE, "scenarios", "render_table.py"), os.path.join(R, "flows.txt")]).decode()
verdict = rd("verdict"); flow_rows = rd("flowsets"); events = rd("events"); gp = rd("goodput"); exs = rd("executor_summary")
arm = txt("arm.txt")


def counter_delta(pre, post):
    a = dict(l.split("=") for l in txt(pre).splitlines() if "=" in l)
    b = dict(l.split("=") for l in txt(post).splitlines() if "=" in l)
    out = []
    for k in b:
        try:
            out.append("%s +%d" % (k, int(b[k]) - int(a.get(k, 0))))
        except ValueError:
            pass
    return "，".join(out) or "无"


def switch_delta():
    """Per port: ECN-marked frames and buffer discards on TC0 between the two
    switch snapshots (frame counts; the table view of nv show mislabels them)."""
    def parse(name):
        d, port = {}, None
        for l in txt(name).splitlines():
            if l.startswith("## "):
                port = l[3:].strip(); continue
            f = l.split()
            if port and len(f) == 6 and f[0] == "0":
                d[port] = (int(f[1]), int(f[3]), int(f[4]))
        return d
    a, b = parse("switch_pre.txt"), parse("switch_post.txt")
    out = []
    for p in b:
        if p in a:
            fr, disc, ecn = (b[p][i] - a[p][i] for i in range(3))
            if fr:
                out.append(f"{p}: {fr} 帧，ECN 标记 {ecn}，缓冲丢弃 {disc}")
    return "；".join(out) or "无（快照缺失或端口无流量）"


keyp = {k: ep[k] for k in ("law", "k", "delta_demand", "d_repay_s", "headroom", "rdma_push_ms", "split_by_sender", "rdma_unknown_rate_bps") if k in ep}
overall = all(v["pass"] == "PASS" for v in verdict)
md = [f"# {tag}", "",
      f"**判定：{'通过' if overall else '不通过'}**（六条判据见下）。执行面臂：`{arm}`；账本参数 `{json.dumps(keyp, ensure_ascii=False)}`。", "",
      "## 环境（运行时实际读到的）", "",
      "| 项 | 取值 |", "|---|---|",
      "| 政策 | 每 VM 权重 1、max_rate 50 G、类 tcp:rdma 1:1、per-sender 全 1（runner 已校验）；C′ = 184 G，每 VM 46 G |",
      "| lab_env 状态 | " + "；".join(env_lines).replace("|", "/") + " |",
      "| 重传 | " + (txt("retrans_mode.txt").splitlines()[-1].replace("|", "/") if txt("retrans_mode.txt") else "未记录") + " |",
      "| 打流器 | RDMA ib_write_bw（perftest-enhanced，`--start_at`，`-m 1024 --report_gbits`）；TCP iperf3 `-P n -J --start-at`，`-B ip%dpu1vfN` |",
      "| 计时 | T0 = " + txt("t0.txt") + "，预热 " + txt("warm.txt") + " s，实验时钟 = T0 + 预热 |", "",
      "## 打流表", "", flows_tbl,
      "## 预期与实测（每阶段稳态窗口 = 阶段起 +5 s 到阶段止 −1 s，接收端归因速率）", "",
      "| 阶段 (s) | 流集合 | 应得 (G) | 实测均值 (G) | 标准差 | 偏差 |", "|---|---|---|---|---|---|"]
for r in flow_rows:
    e, m = float(r["expected_gbps"]), float(r["attributed_mean_gbps"])
    md.append(f"| {r['phase']} | {r['fsid']} | {e:.2f} | {m:.2f} | {float(r['attributed_sd_gbps']):.2f} | {100*(m-e)/e if e else 0:+.1f}% |")
md += ["", "## 应用自报 goodput（整条流的均值；TCP = 接收字节 ÷ 客户端测试时长）", "", "| 行 | 流集合 | 起–止 (s) | goodput (G) |", "|---|---|---|---|"]
for r in gp:
    md.append(f"| {r['row']} | {r['fsid']} | {r['start_s']}–{r['end_s']} | {r['goodput_gbps'] or '—'} |")
md += ["", "## 收敛（事件后进入新应得 ±10% 且保持 1 s 的首个时刻）", "", "| 事件 (s) | 类 | 涉及流集合 | 收敛时间 (ms) |", "|---|---|---|---|"]
for r in events:
    md.append(f"| {r['event_s']} | {r['class']} | {r['flowsets']} | {r['converge_ms'] or '未收敛'} |")
md += ["", "## 六条判据", "", "| 判据 | 结果 | 说明 |", "|---|---|---|"]
for v in verdict:
    md.append(f"| {v['criterion']} | {'通过' if v['pass']=='PASS' else '不通过'} | {v['detail']} |")
md += ["", "## 机制旁证", "",
       "RDMA 执行面的自述（每秒一次设备回读，稳态窗口内的均值）：", "",
       "| 流集合 | 样本 | 已整形/R | 取令牌的 QP 的 CC 之和/R | 全员取令牌样本里已整形/R 的均值 / 95 分位（样本数） | CC 之和 ≥ R 的样本里已整形 ≥ 0.95 R 的比例 |", "|---|---|---|---|---|---|"]
for r in exs:
    md.append(f"| {r['fsid']} | {r['samples']} | {float(r['mean_paced_over_R']):.3f} | {float(r['mean_cc_live_over_R']):.3f} | {('%s / %s (%s)' % (r['all_live_mean_paced_over_R'], r['all_live_p95_paced_over_R'], r['all_live_samples'])) if r['all_live_mean_paced_over_R'] else '—'} | {('%.0f%%' % (100*float(r['full_frac']))) if r['full_frac'] else '—（CC 从未要到 R）'} |")
if not exs:
    md.append("| —（无执行面样本）| | | | | |")
md += ["", f"CNP/ECN 计数增量：{counter_delta('cnp_pre.txt', 'cnp_post.txt')}。", "",
       f"交换机出向队列 TC0 增量：{switch_delta()}。", "",
       "## 图与数据", "",
       f"`fig/{tag.split('_')[0]}_timeline.png`（每次运行覆盖）：上图每个流集合的归因速率与应得（黑点线），中图接收端网卡按 VM 和类的 wire 速率堆叠，下图虚拟队列。`fig/{tag.split('_')[0]}_executor.png`：RDMA 执行面每流集合的 R、取令牌 QP 的 CC 之和、已整形之和。数据：`data/{tag}_vmclass.csv`（100 ms wire）、`data/{tag}_flowsets.csv`、`data/{tag}_goodput.csv`、`data/{tag}_events.csv`、`data/{tag}_executor.csv`、`data/{tag}_executor_summary.csv`、`data/{tag}_verdict.csv`。原始数据 `results/{tag}/`。", ""]
open(os.path.join(O, f"{tag}.md"), "w").write("\n".join(md))
print("ok", "PASS" if overall else "FAIL")
