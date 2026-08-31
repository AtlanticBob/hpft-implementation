# V2_startcert_20260830_rep3

**判定：不通过**（五条判据见下）。被测对象 `law=conf`，参数 `{"law": "conf", "k": 20.0, "delta_demand": 0.15, "d_repay_s": 0.3, "trust_recover_s": 1.0, "headroom": 0.08, "rdma_push_ms": 5, "split_by_sender": true, "rdma_unknown_rate_bps": 5000000000.0}`。

## 环境（运行时实际读到的）

| 项 | 取值 |
|---|---|
| 政策 | 每 VM 权重 1、max_rate 50 G、类 tcp:rdma 1:1、per-sender 全 1（runner 已校验）；C′ = 184 G，每 VM 46 G |
| lab_env 状态 | sgpu01  hpft-dpu   UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu02  hpft-dpu2  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu03  hpft-dpu3  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu04  hpft-dpu4  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；HOST     DPU         RXAGENT   TXAGENT   VPORTMETER  SHIM      QPNRES；sgpu01   hpft-dpu    inactive  active    active      active    active；sgpu02   hpft-dpu2   active    inactive  active      inactive  inactive；sgpu03   hpft-dpu3   inactive  active    active      active    active；sgpu04   hpft-dpu4   inactive  inactive  active      inactive  inactive；swp37s0 ECN profile: motiv_default_ecn；==> environment: HPFT |
| 打流器 | RDMA ib_write_bw（perftest-26015，`--start_at`，`-m 1024 --report_gbits`）；TCP iperf3 `-P n -b 0 -J`，`-B ip%dpu1vfN` |
| 计时 | T0 = 1788157193，预热 5 s，实验时钟 = T0 + 预热 |

## 打流表

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 30 | 60 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 30 | 60 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 30 | 60 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 30 | 60 |  |

## 预期与实测（每阶段稳态窗口 = 阶段起 +5 s 到阶段止 −1 s，接收端归因速率）

| 阶段 (s) | 流集合 | 应得 (G) | 实测均值 (G) | 标准差 | 偏差 |
|---|---|---|---|---|---|
| 0-30 | sgpu01/vf0>sgpu02/vf0|rdma | 23.00 | 22.28 | 0.14 | -3.1% |
| 0-30 | sgpu01/vf0>sgpu02/vf0|tcp | 23.00 | 22.63 | 0.15 | -1.6% |
| 0-30 | sgpu01/vf1>sgpu02/vf1|rdma | 23.00 | 22.27 | 0.19 | -3.2% |
| 0-30 | sgpu01/vf1>sgpu02/vf1|tcp | 23.00 | 22.63 | 0.20 | -1.6% |
| 0-30 | sgpu01/vf2>sgpu02/vf2|rdma | 23.00 | 22.27 | 0.17 | -3.2% |
| 0-30 | sgpu01/vf2>sgpu02/vf2|tcp | 23.00 | 22.63 | 0.17 | -1.6% |
| 0-30 | sgpu01/vf3>sgpu02/vf3|rdma | 23.00 | 22.28 | 0.14 | -3.1% |
| 0-30 | sgpu01/vf3>sgpu02/vf3|tcp | 23.00 | 22.63 | 0.15 | -1.6% |
| 30-60 | sgpu01/vf0>sgpu02/vf0|rdma | 11.50 | 11.49 | 0.15 | -0.1% |
| 30-60 | sgpu03/vf0>sgpu02/vf0|rdma | 11.50 | 11.50 | 0.15 | +0.0% |
| 30-60 | sgpu01/vf0>sgpu02/vf0|tcp | 11.50 | 11.49 | 0.15 | -0.1% |
| 30-60 | sgpu03/vf0>sgpu02/vf0|tcp | 11.50 | 11.49 | 0.15 | -0.1% |
| 30-60 | sgpu01/vf1>sgpu02/vf1|rdma | 11.50 | 11.49 | 0.23 | -0.1% |
| 30-60 | sgpu03/vf1>sgpu02/vf1|rdma | 11.50 | 11.50 | 0.23 | +0.0% |
| 30-60 | sgpu01/vf1>sgpu02/vf1|tcp | 11.50 | 11.49 | 0.23 | -0.1% |
| 30-60 | sgpu03/vf1>sgpu02/vf1|tcp | 11.50 | 11.50 | 0.22 | +0.0% |
| 30-60 | sgpu01/vf2>sgpu02/vf2|rdma | 11.50 | 11.50 | 0.10 | +0.0% |
| 30-60 | sgpu03/vf2>sgpu02/vf2|rdma | 11.50 | 11.49 | 0.10 | -0.1% |
| 30-60 | sgpu01/vf2>sgpu02/vf2|tcp | 11.50 | 11.49 | 0.11 | -0.1% |
| 30-60 | sgpu03/vf2>sgpu02/vf2|tcp | 11.50 | 11.49 | 0.11 | -0.1% |
| 30-60 | sgpu01/vf3>sgpu02/vf3|rdma | 11.50 | 11.49 | 0.14 | -0.1% |
| 30-60 | sgpu03/vf3>sgpu02/vf3|rdma | 11.50 | 11.49 | 0.15 | -0.1% |
| 30-60 | sgpu01/vf3>sgpu02/vf3|tcp | 11.50 | 11.49 | 0.15 | -0.1% |
| 30-60 | sgpu03/vf3>sgpu02/vf3|tcp | 11.50 | 11.49 | 0.16 | -0.1% |
| 60-90 | sgpu01/vf0>sgpu02/vf0|rdma | 23.00 | 22.21 | 1.04 | -3.4% |
| 60-90 | sgpu01/vf0>sgpu02/vf0|tcp | 23.00 | 22.65 | 0.27 | -1.5% |
| 60-90 | sgpu01/vf1>sgpu02/vf1|rdma | 23.00 | 22.21 | 1.07 | -3.4% |
| 60-90 | sgpu01/vf1>sgpu02/vf1|tcp | 23.00 | 22.65 | 0.26 | -1.5% |
| 60-90 | sgpu01/vf2>sgpu02/vf2|rdma | 23.00 | 22.22 | 0.86 | -3.4% |
| 60-90 | sgpu01/vf2>sgpu02/vf2|tcp | 23.00 | 22.66 | 0.27 | -1.5% |
| 60-90 | sgpu01/vf3>sgpu02/vf3|rdma | 23.00 | 22.21 | 0.93 | -3.4% |
| 60-90 | sgpu01/vf3>sgpu02/vf3|tcp | 23.00 | 22.66 | 0.31 | -1.5% |

## 应用自报 goodput（整条流的均值）

| 行 | 流集合 | 起–止 (s) | goodput (G) |
|---|---|---|---|
| 0 | sgpu01/vf0>sgpu02/vf0|rdma | 0–90 | 14.54 |
| 1 | sgpu01/vf0>sgpu02/vf0|tcp | 0–90 | 18.12 |
| 2 | sgpu01/vf1>sgpu02/vf1|rdma | 0–90 | 14.54 |
| 3 | sgpu01/vf1>sgpu02/vf1|tcp | 0–90 | 18.12 |
| 4 | sgpu01/vf2>sgpu02/vf2|rdma | 0–90 | 14.54 |
| 5 | sgpu01/vf2>sgpu02/vf2|tcp | 0–90 | 18.12 |
| 6 | sgpu01/vf3>sgpu02/vf3|rdma | 0–90 | 14.54 |
| 7 | sgpu01/vf3>sgpu02/vf3|tcp | 0–90 | 18.12 |
| 8 | sgpu03/vf0>sgpu02/vf0|rdma | 30–60 | 10.72 |
| 9 | sgpu03/vf0>sgpu02/vf0|tcp | 30–60 | 10.86 |
| 10 | sgpu03/vf1>sgpu02/vf1|rdma | 30–60 | 10.72 |
| 11 | sgpu03/vf1>sgpu02/vf1|tcp | 30–60 | 10.85 |
| 12 | sgpu03/vf2>sgpu02/vf2|rdma | 30–60 | 10.72 |
| 13 | sgpu03/vf2>sgpu02/vf2|tcp | 30–60 | 10.86 |
| 14 | sgpu03/vf3>sgpu02/vf3|rdma | 30–60 | 10.72 |
| 15 | sgpu03/vf3>sgpu02/vf3|tcp | 30–60 | 10.85 |

## 收敛（事件后进入新应得 ±10% 且保持 1 s 的首个时刻）

| 事件 (s) | 类 | 涉及流集合 | 收敛时间 (ms) |
|---|---|---|---|
| 30 | rdma | 8 | 1090 |
| 30 | tcp | 8 | 1150 |
| 60 | rdma | 4 | 779 |
| 60 | tcp | 4 | 779 |

## 五条判据

| 判据 | 结果 | 说明 |
|---|---|---|
| 1 稳态贴合 | 通过 | ok |
| 2 队列消得掉 | 通过 | ok |
| 3 收敛 ≤ 1 s | 不通过 | 30s rdma x8 1090 ms; 30s tcp x8 1150 ms |
| 4 不塌 | 通过 | ok |
| 5 平台健康 | 通过 | ok |

## 机制旁证

CNP/ECN 计数增量：sgpu01.mlx5_3.rp_cnp_handled +290486，sgpu01.mlx5_3.rp_cnp_ignored +0，sgpu03.mlx5_3.rp_cnp_handled +239743，sgpu03.mlx5_3.rp_cnp_ignored +0，sgpu02.mlx5_3.np_cnp_sent +530077，sgpu02.mlx5_3.np_ecn_marked_roce_packets +8849152。TCP 执行面跑后信任度最大值 0.000（0 个流对）。交换机 swp37s0 队列计数快照在 `results/V2_startcert_20260830_rep3/switch_pre.txt` 与 `switch_post.txt`。

丢包与信任（设计 v4 §6.3 第三步）：RDMA 执行面 7 个流集合共收到 940 次 NACK，丢包快速通道放行信任上升 1299 个周期，信任度峰值 0.0769；TCP 执行面 8 个流集合中 8 个出现过重传，快速通道放行 88 个周期，信任度峰值 0.0008。快速通道只在队列为空且拥塞控制的额度低于围栏时才放行，所以有重传不等于会放行。

## 图与数据

`fig/V2_timeline.png`（每次运行覆盖）：上图每个流集合的归因速率与应得（黑点线），中图接收端网卡按 VM 和类的 wire 速率堆叠，下图虚拟队列。数据：`data/V2_startcert_20260830_rep3_vmclass.csv`（100 ms wire）、`data/V2_startcert_20260830_rep3_flowsets.csv`、`data/V2_startcert_20260830_rep3_goodput.csv`、`data/V2_startcert_20260830_rep3_events.csv`、`data/V2_startcert_20260830_rep3_verdict.csv`。原始数据 `results/V2_startcert_20260830_rep3/`。
