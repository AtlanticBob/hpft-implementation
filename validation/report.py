#!/usr/bin/env python3
"""reports/<tag>.md from data/ and the run's environment snapshots.
Sections follow README §五: what was run (environment as it actually was, the
law under test), the flow table, expected vs measured per phase, convergence
per event, the five verdicts, mechanism evidence (CNP, switch queues, trust),
figures. usage: report.py <tag>"""
import csv, json, os, re, subprocess, sys
BASE = os.path.dirname(os.path.abspath(__file__))
tag = sys.argv[1]
R = os.path.join(BASE, "results", tag); D = os.path.join(BASE, "data"); O = os.path.join(BASE, "reports")
os.makedirs(O, exist_ok=True)


def rd(name):
    with open(os.path.join(D, f"{tag}_{name}.csv")) as f:
        return list(csv.DictReader(f))


def txt(name):
    p = os.path.join(R, name)
    return open(p).read().strip() if os.path.exists(p) else ""


reg = json.load(open(os.path.join(R, "registry.json")))
ep = reg["e_params"]
env = txt("env_status.txt")
env_lines = [l for l in env.splitlines() if re.search(r"UPCC|ECN profile|environment:|^HOST|^sgpu0[1-4]\s", l)]
flows_tbl = subprocess.check_output([sys.executable, os.path.join(BASE, "scenarios", "render_table.py"), os.path.join(R, "flows.txt")]).decode()
scn = txt("flows.txt").splitlines()
verdict = rd("verdict"); flow_rows = rd("flowsets"); events = rd("events"); gp = rd("goodput")


def cnp_delta():
    pre = dict(l.split("=") for l in txt("cnp_pre.txt").splitlines() if "=" in l)
    post = dict(l.split("=") for l in txt("cnp_post.txt").splitlines() if "=" in l)
    out = []
    for k in post:
        try:
            out.append("%s +%d" % (k, int(post[k]) - int(pre.get(k, 0))))
        except ValueError:
            pass
    return "，".join(out) or "无"


trust = txt("trust_post.txt").splitlines()
tmax = max([float(re.search(r"trust=([\d.]+)", l).group(1)) for l in trust if "trust=" in l] or [0.0])


def loss_line():
    """Design v4 6.3 step three: did the network drop anything, and did the
    loss fast path hand any flow-set back to its own CC? Both executors
    count it per flow-set - the RDMA one in the device readback (nack,
    loss_ep), the TCP one in its BPF map (loss_age_ms, loss_ep)."""
    import glob
    rd_n = rd_ep = rd_pairs = 0
    rd_tmax = 0.0
    for f in glob.glob(os.path.join(R, "rp_*.jsonl")):
        per = {}
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = per.setdefault(r["ft"], [0, 0, 0.0])
            d[0] = max(d[0], r.get("nack", 0))
            d[1] = max(d[1], r.get("loss_ep", 0))
            d[2] = max(d[2], r.get("trust", 0.0))
        for d in per.values():
            rd_pairs += 1; rd_n += d[0]; rd_ep += d[1]; rd_tmax = max(rd_tmax, d[2])
    tc_pairs = tc_lost = tc_ep = 0
    tc_tmax = 0.0
    for f in glob.glob(os.path.join(R, "trust_*.jsonl")):
        per = {}
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = per.setdefault(r["key"], [False, 0, 0.0])
            d[0] = d[0] or r.get("loss_age_ms", -1) >= 0
            d[1] = max(d[1], r.get("loss_ep", 0))
            d[2] = max(d[2], r.get("trust", 0.0))
        for d in per.values():
            tc_pairs += 1; tc_lost += bool(d[0]); tc_ep += d[1]; tc_tmax = max(tc_tmax, d[2])
    if not rd_pairs and not tc_pairs:
        return "执行面采样缺失，无法判断。"
    return (f"RDMA 执行面 {rd_pairs} 个流集合共收到 {rd_n} 次 NACK，丢包快速通道放行信任上升 "
            f"{rd_ep} 个周期，信任度峰值 {rd_tmax:.4f}；TCP 执行面 {tc_pairs} 个流集合中 "
            f"{tc_lost} 个出现过重传，快速通道放行 {tc_ep} 个周期，信任度峰值 {tc_tmax:.4f}。"
            "快速通道只在队列为空且拥塞控制的额度低于围栏时才放行，所以有重传不等于会放行。")
law = ep.get("law"); keyp = {k: ep[k] for k in ("law", "k", "delta_demand", "d_repay_s", "trust_recover_s", "headroom", "rdma_push_ms", "split_by_sender", "rdma_unknown_rate_bps") if k in ep}
overall = all(v["pass"] == "PASS" for v in verdict)
md = [f"# {tag}", "",
      f"**判定：{'通过' if overall else '不通过'}**（五条判据见下）。被测对象 `law={law}`，参数 `{json.dumps(keyp, ensure_ascii=False)}`。", "",
      "## 环境（运行时实际读到的）", "",
      "| 项 | 取值 |", "|---|---|",
      "| 政策 | 每 VM 权重 1、max_rate 50 G、类 tcp:rdma 1:1、per-sender 全 1（runner 已校验）；C′ = 184 G，每 VM 46 G |",
      "| lab_env 状态 | " + "；".join(env_lines).replace("|", "/") + " |",
      "| 打流器 | RDMA ib_write_bw（perftest-26015，`--start_at`，`-m 1024 --report_gbits`）；TCP iperf3 `-P n -b 0 -J`，`-B ip%dpu1vfN` |",
      "| 计时 | T0 = " + txt("t0.txt") + "，预热 " + txt("warm.txt") + " s，实验时钟 = T0 + 预热 |", "",
      "## 打流表", "", flows_tbl,
      "## 预期与实测（每阶段稳态窗口 = 阶段起 +5 s 到阶段止 −1 s，接收端归因速率）", "",
      "| 阶段 (s) | 流集合 | 应得 (G) | 实测均值 (G) | 标准差 | 偏差 |", "|---|---|---|---|---|---|"]
for r in flow_rows:
    e, m = float(r["expected_gbps"]), float(r["attributed_mean_gbps"])
    md.append(f"| {r['phase']} | {r['fsid']} | {e:.2f} | {m:.2f} | {float(r['attributed_sd_gbps']):.2f} | {100*(m-e)/e if e else 0:+.1f}% |")
md += ["", "## 应用自报 goodput（整条流的均值）", "", "| 行 | 流集合 | 起–止 (s) | goodput (G) |", "|---|---|---|---|"]
for r in gp:
    md.append(f"| {r['row']} | {r['fsid']} | {r['start_s']}–{r['end_s']} | {r['goodput_gbps'] or '—'} |")
md += ["", "## 收敛（事件后进入新应得 ±10% 且保持 1 s 的首个时刻）", "", "| 事件 (s) | 类 | 涉及流集合 | 收敛时间 (ms) |", "|---|---|---|---|"]
for r in events:
    md.append(f"| {r['event_s']} | {r['class']} | {r['flowsets']} | {r['converge_ms'] or '未收敛'} |")
md += ["", "## 五条判据", "", "| 判据 | 结果 | 说明 |", "|---|---|---|"]
for v in verdict:
    md.append(f"| {v['criterion']} | {'通过' if v['pass']=='PASS' else '不通过'} | {v['detail']} |")
md += ["", "## 机制旁证", "",
       f"CNP/ECN 计数增量：{cnp_delta()}。TCP 执行面跑后信任度最大值 {tmax:.3f}（{len(trust)} 个流对）。交换机 swp37s0 队列计数快照在 `results/{tag}/switch_pre.txt` 与 `switch_post.txt`。", "",
       f"丢包与信任（设计 v4 §6.3 第三步）：{loss_line()}", "",
       "## 图与数据", "",
       f"`fig/{tag.split('_')[0]}_timeline.png`（每次运行覆盖）：上图每个流集合的归因速率与应得（黑点线），中图接收端网卡按 VM 和类的 wire 速率堆叠，下图虚拟队列。数据：`data/{tag}_vmclass.csv`（100 ms wire）、`data/{tag}_flowsets.csv`、`data/{tag}_goodput.csv`、`data/{tag}_events.csv`、`data/{tag}_verdict.csv`。原始数据 `results/{tag}/`。", ""]
open(os.path.join(O, f"{tag}.md"), "w").write("\n".join(md))
print("ok", "PASS" if overall else "FAIL")
