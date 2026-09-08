# V1swift_snap_20260908

**判定：通过**（六条判据见下）。执行面臂：`rdma executor: cc algo = 3 (2 DCQCN, 3 Swift); law = 0 (0 bucket, 1 equal cap, 2 equal split); cc-only arm = 0; knobs = 0xcce 2 23`；账本参数 `{"law": "conf", "k": 20.0, "delta_demand": 0.15, "d_repay_s": 0.3, "headroom": 0.08, "rdma_push_ms": 5, "split_by_sender": true}`。

## 环境（运行时实际读到的）

| 项 | 取值 |
|---|---|
| 政策 | 每 VM 权重 1、max_rate 50 G、类 tcp:rdma 1:1、per-sender 全 1（runner 已校验）；C′ = 184 G，每 VM 46 G |
| lab_env 状态 | sgpu01  hpft-dpu   UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu02  hpft-dpu2  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=0 / p1=200G；sgpu03  hpft-dpu3  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu04  hpft-dpu4  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；HOST     DPU         RXAGENT   TXAGENT   VPORTMETER  SHIM      QPNRES；sgpu01   hpft-dpu    active    active    active      active    active；sgpu02   hpft-dpu2   active    active    active      active    active；sgpu03   hpft-dpu3   active    active    active      active    active；sgpu04   hpft-dpu4   active    active    active      active    active；swp37s0 ECN profile: hpft_ecn；==> environment: HPFT |
| 重传 | retransmission: SR (ROCE_ACCL selective_repeat_forced_en=1 on sgpu01 sgpu02 sgpu03 sgpu04 ) |
| 打流器 | RDMA ib_write_bw（perftest-enhanced，`--start_at`，`-m 1024 --report_gbits`）；TCP iperf3 `-P n -J --start-at`，`-B ip%dpu1vfN` |
| 计时 | T0 = 1788857730，预热 5 s，实验时钟 = T0 + 预热 |

## 打流表

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 17 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 18 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 19 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 20 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 21 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 22 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 23 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 24 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |

## 预期与实测（每阶段稳态窗口 = 阶段起 +5 s 到阶段止 −1 s，接收端归因速率）

| 阶段 (s) | 流集合 | 应得 (G) | 实测均值 (G) | 标准差 | 偏差 |
|---|---|---|---|---|---|
| 0-20 | sgpu01/vf0>sgpu02/vf0|rdma | 7.67 | 7.60 | 0.47 | -0.9% |
| 0-20 | sgpu03/vf0>sgpu02/vf0|rdma | 7.67 | 7.57 | 0.44 | -1.3% |
| 0-20 | sgpu04/vf0>sgpu02/vf0|rdma | 7.67 | 7.57 | 0.48 | -1.3% |
| 0-20 | sgpu01/vf0>sgpu02/vf0|tcp | 7.67 | 7.63 | 0.38 | -0.5% |
| 0-20 | sgpu03/vf0>sgpu02/vf0|tcp | 7.67 | 7.62 | 0.61 | -0.7% |
| 0-20 | sgpu04/vf0>sgpu02/vf0|tcp | 7.67 | 7.62 | 0.60 | -0.7% |
| 0-20 | sgpu01/vf1>sgpu02/vf1|rdma | 7.67 | 7.41 | 0.85 | -3.4% |
| 0-20 | sgpu03/vf1>sgpu02/vf1|rdma | 7.67 | 7.41 | 0.78 | -3.4% |
| 0-20 | sgpu04/vf1>sgpu02/vf1|rdma | 7.67 | 7.45 | 0.80 | -2.9% |
| 0-20 | sgpu01/vf1>sgpu02/vf1|tcp | 7.67 | 7.66 | 0.36 | -0.1% |
| 0-20 | sgpu03/vf1>sgpu02/vf1|tcp | 7.67 | 7.59 | 0.37 | -1.0% |
| 0-20 | sgpu04/vf1>sgpu02/vf1|tcp | 7.67 | 7.63 | 0.32 | -0.5% |
| 0-20 | sgpu01/vf2>sgpu02/vf2|rdma | 7.67 | 7.43 | 0.84 | -3.1% |
| 0-20 | sgpu03/vf2>sgpu02/vf2|rdma | 7.67 | 7.40 | 0.77 | -3.5% |
| 0-20 | sgpu04/vf2>sgpu02/vf2|rdma | 7.67 | 7.41 | 0.78 | -3.4% |
| 0-20 | sgpu01/vf2>sgpu02/vf2|tcp | 7.67 | 7.63 | 0.35 | -0.5% |
| 0-20 | sgpu03/vf2>sgpu02/vf2|tcp | 7.67 | 7.64 | 0.33 | -0.4% |
| 0-20 | sgpu04/vf2>sgpu02/vf2|tcp | 7.67 | 7.64 | 0.37 | -0.4% |
| 0-20 | sgpu01/vf3>sgpu02/vf3|rdma | 7.67 | 7.48 | 0.83 | -2.5% |
| 0-20 | sgpu03/vf3>sgpu02/vf3|rdma | 7.67 | 7.38 | 0.75 | -3.8% |
| 0-20 | sgpu04/vf3>sgpu02/vf3|rdma | 7.67 | 7.40 | 0.84 | -3.5% |
| 0-20 | sgpu01/vf3>sgpu02/vf3|tcp | 7.67 | 7.66 | 0.67 | -0.1% |
| 0-20 | sgpu03/vf3>sgpu02/vf3|tcp | 7.67 | 7.60 | 0.42 | -0.9% |
| 0-20 | sgpu04/vf3>sgpu02/vf3|tcp | 7.67 | 7.61 | 0.48 | -0.8% |

## 应用自报 goodput（整条流的均值；TCP = 接收字节 ÷ 客户端测试时长）

| 行 | 流集合 | 起–止 (s) | goodput (G) |
|---|---|---|---|
| 0 | sgpu01/vf0>sgpu02/vf0|rdma | 0–20 | 6.96 |
| 1 | sgpu01/vf0>sgpu02/vf0|tcp | 0–20 | 7.48 |
| 2 | sgpu01/vf1>sgpu02/vf1|rdma | 0–20 | 6.82 |
| 3 | sgpu01/vf1>sgpu02/vf1|tcp | 0–20 | 7.48 |
| 4 | sgpu01/vf2>sgpu02/vf2|rdma | 0–20 | 6.83 |
| 5 | sgpu01/vf2>sgpu02/vf2|tcp | 0–20 | 7.50 |
| 6 | sgpu01/vf3>sgpu02/vf3|rdma | 0–20 | 6.83 |
| 7 | sgpu01/vf3>sgpu02/vf3|tcp | 0–20 | 7.49 |
| 8 | sgpu03/vf0>sgpu02/vf0|rdma | 0–20 | 6.98 |
| 9 | sgpu03/vf0>sgpu02/vf0|tcp | 0–20 | 7.49 |
| 10 | sgpu03/vf1>sgpu02/vf1|rdma | 0–20 | 6.88 |
| 11 | sgpu03/vf1>sgpu02/vf1|tcp | 0–20 | 7.47 |
| 12 | sgpu03/vf2>sgpu02/vf2|rdma | 0–20 | 6.81 |
| 13 | sgpu03/vf2>sgpu02/vf2|tcp | 0–20 | 7.49 |
| 14 | sgpu03/vf3>sgpu02/vf3|rdma | 0–20 | 6.89 |
| 15 | sgpu03/vf3>sgpu02/vf3|tcp | 0–20 | 7.49 |
| 16 | sgpu04/vf0>sgpu02/vf0|rdma | 0–20 | 6.95 |
| 17 | sgpu04/vf0>sgpu02/vf0|tcp | 0–20 | 7.49 |
| 18 | sgpu04/vf1>sgpu02/vf1|rdma | 0–20 | 6.81 |
| 19 | sgpu04/vf1>sgpu02/vf1|tcp | 0–20 | 7.49 |
| 20 | sgpu04/vf2>sgpu02/vf2|rdma | 0–20 | 6.83 |
| 21 | sgpu04/vf2>sgpu02/vf2|tcp | 0–20 | 7.50 |
| 22 | sgpu04/vf3>sgpu02/vf3|rdma | 0–20 | 6.83 |
| 23 | sgpu04/vf3>sgpu02/vf3|tcp | 0–20 | 7.46 |

## 收敛（事件后进入新应得 ±10% 且保持 1 s 的首个时刻）

| 事件 (s) | 类 | 涉及流集合 | 收敛时间 (ms) |
|---|---|---|---|

## 六条判据

| 判据 | 结果 | 说明 |
|---|---|---|
| 1 稳态贴合 | 通过 | ok |
| 2 队列消得掉 | 通过 | ok |
| 3 收敛 ≤ 1 s | 通过 | ok |
| 4 不塌 | 通过 | ok |
| 5 平台健康 | 通过 | ok |
| 6 执行面守约 | 通过 | ok |

## 机制旁证

RDMA 执行面的自述（每秒一次设备回读，稳态窗口内的均值）：

| 流集合 | 样本 | 已整形/R | 取令牌的 QP 的 CC 之和/R | 全员取令牌样本里已整形/R 的均值 / 95 分位（样本数） | CC 之和 ≥ R 的样本里已整形 ≥ 0.95 R 的比例 |
|---|---|---|---|---|---|
| sgpu01/vf0>sgpu02/vf0|rdma | 14 | 1.046 | 31.425 | 1.000 / 1.000 (12) | 100% |
| sgpu01/vf1>sgpu02/vf1|rdma | 14 | 1.193 | 15.428 | 1.000 / 1.000 (6) | 100% |
| sgpu01/vf2>sgpu02/vf2|rdma | 14 | 0.996 | 27.903 | 1.000 / 1.000 (11) | 93% |
| sgpu01/vf3>sgpu02/vf3|rdma | 14 | 1.087 | 24.290 | 1.000 / 1.000 (8) | 100% |
| sgpu03/vf0>sgpu02/vf0|rdma | 14 | 1.056 | 35.200 | 1.000 / 1.000 (12) | 100% |
| sgpu03/vf1>sgpu02/vf1|rdma | 14 | 1.033 | 23.527 | 0.999 / 1.000 (9) | 100% |
| sgpu03/vf2>sgpu02/vf2|rdma | 14 | 1.089 | 22.306 | 1.000 / 1.000 (7) | 100% |
| sgpu03/vf3>sgpu02/vf3|rdma | 14 | 1.177 | 19.887 | 1.000 / 1.000 (6) | 93% |
| sgpu04/vf0>sgpu02/vf0|rdma | 14 | 1.000 | 23.675 | 1.000 / 1.000 (12) | 100% |
| sgpu04/vf1>sgpu02/vf1|rdma | 14 | 1.149 | 37.850 | 1.000 / 1.000 (8) | 100% |
| sgpu04/vf2>sgpu02/vf2|rdma | 14 | 1.314 | 19.155 | 1.000 / 1.000 (6) | 100% |
| sgpu04/vf3>sgpu02/vf3|rdma | 14 | 1.097 | 17.508 | 1.000 / 1.000 (11) | 100% |

CNP/ECN 计数增量：sgpu01.mlx5_3.rp_cnp_handled +62150，sgpu01.mlx5_3.rp_cnp_ignored +0，sgpu03.mlx5_3.rp_cnp_handled +62147，sgpu03.mlx5_3.rp_cnp_ignored +0，sgpu04.mlx5_3.rp_cnp_handled +62112，sgpu04.mlx5_3.rp_cnp_ignored +0，sgpu02.mlx5_3.np_cnp_sent +186401，sgpu02.mlx5_3.np_ecn_marked_roce_packets +316679。

交换机出向队列 TC0 增量：swp37s0: 473581361 帧，ECN 标记 316585，缓冲丢弃 0；swp4s1: 14228677 帧，ECN 标记 0，缓冲丢弃 0。

## 图与数据

`fig/V1swift_timeline.png`（每次运行覆盖）：上图每个流集合的归因速率与应得（黑点线），中图接收端网卡按 VM 和类的 wire 速率堆叠，下图虚拟队列。`fig/V1swift_executor.png`：RDMA 执行面每流集合的 R、取令牌 QP 的 CC 之和、已整形之和。数据：`data/V1swift_snap_20260908_vmclass.csv`（100 ms wire）、`data/V1swift_snap_20260908_flowsets.csv`、`data/V1swift_snap_20260908_goodput.csv`、`data/V1swift_snap_20260908_events.csv`、`data/V1swift_snap_20260908_executor.csv`、`data/V1swift_snap_20260908_executor_summary.csv`、`data/V1swift_snap_20260908_verdict.csv`。原始数据 `results/V1swift_snap_20260908/`。
