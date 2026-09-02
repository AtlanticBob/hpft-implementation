# rdmapair_E_20260831_r1

**判定：不通过**（五条判据见下）。被测对象 `law=conf`，参数 `{"law": "conf", "k": 20.0, "delta_demand": 0.15, "d_repay_s": 0.3, "trust_recover_s": 1.0, "headroom": 0.08, "rdma_push_ms": 5, "split_by_sender": true, "rdma_unknown_rate_bps": 5000000000.0}`。

## 环境（运行时实际读到的）

| 项 | 取值 |
|---|---|
| 政策 | 每 VM 权重 1、max_rate 50 G、类 tcp:rdma 1:1、per-sender 全 1（runner 已校验）；C′ = 184 G，每 VM 46 G |
| lab_env 状态 | sgpu01  hpft-dpu   UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu02  hpft-dpu2  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu03  hpft-dpu3  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；sgpu04  hpft-dpu4  UPCC current=1 next=1 / SR current=0 next=0 / doca_pcc=1 / p1=200G；HOST     DPU         RXAGENT   TXAGENT   VPORTMETER  SHIM      QPNRES；sgpu01   hpft-dpu    inactive  active    active      active    active；sgpu02   hpft-dpu2   active    inactive  active      inactive  inactive；sgpu03   hpft-dpu3   inactive  active    active      active    active；sgpu04   hpft-dpu4   inactive  inactive  active      inactive  inactive；swp37s0 ECN profile: motiv_default_ecn；==> environment: HPFT |
| 打流器 | RDMA ib_write_bw（perftest-26015，`--start_at`，`-m 1024 --report_gbits`）；TCP iperf3 `-P n -b 0 -J`，`-B ip%dpu1vfN` |
| 计时 | T0 = 1788170738，预热 5 s，实验时钟 = T0 + 预热 |

## 打流表

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 10 QP | 0 | 30 |  |
| 2 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 10 QP | 0 | 30 |  |

## 预期与实测（每阶段稳态窗口 = 阶段起 +5 s 到阶段止 −1 s，接收端归因速率）

| 阶段 (s) | 流集合 | 应得 (G) | 实测均值 (G) | 标准差 | 偏差 |
|---|---|---|---|---|---|
| 0-30 | sgpu01/vf0>sgpu02/vf0|rdma | 25.00 | 24.64 | 1.51 | -1.4% |
| 0-30 | sgpu03/vf0>sgpu02/vf0|rdma | 25.00 | 24.62 | 1.46 | -1.5% |

## 应用自报 goodput（整条流的均值）

| 行 | 流集合 | 起–止 (s) | goodput (G) |
|---|---|---|---|
| 0 | sgpu01/vf0>sgpu02/vf0|rdma | 0–30 | 23.07 |
| 1 | sgpu03/vf0>sgpu02/vf0|rdma | 0–30 | 23.01 |

## 收敛（事件后进入新应得 ±10% 且保持 1 s 的首个时刻）

| 事件 (s) | 类 | 涉及流集合 | 收敛时间 (ms) |
|---|---|---|---|

## 五条判据

| 判据 | 结果 | 说明 |
|---|---|---|
| 1 稳态贴合 | 通过 | ok |
| 2 队列消得掉 | 不通过 | 0-30s mean 1.63 ms |
| 3 收敛 ≤ 1 s | 通过 | ok |
| 4 不塌 | 通过 | ok |
| 5 平台健康 | 通过 | ok |

## 机制旁证

CNP/ECN 计数增量：sgpu01.mlx5_3.rp_cnp_handled +0，sgpu01.mlx5_3.rp_cnp_ignored +0，sgpu03.mlx5_3.rp_cnp_handled +0，sgpu03.mlx5_3.rp_cnp_ignored +0，sgpu02.mlx5_3.np_cnp_sent +0，sgpu02.mlx5_3.np_ecn_marked_roce_packets +0。TCP 执行面跑后置信度最大值 0.000（196 个流对）。交换机 swp37s0 队列计数快照在 `results/rdmapair_E_20260831_r1/switch_pre.txt` 与 `switch_post.txt`。

丢包与置信度（设计 v4 §6.3 第三步）：RDMA 执行面 2 个流集合共收到 0 次 NACK，丢包快速通道放行置信度上升 0 个周期，置信度峰值 0.0000；TCP 执行面 8 个流集合中 8 个出现过重传，快速通道放行 164 个周期，置信度峰值 0.0003。快速通道只在队列为空且拥塞控制的额度低于围栏时才放行，所以有重传不等于会放行。

## 图与数据

`fig/rdmapair_timeline.png`（每次运行覆盖）：上图每个流集合的归因速率与应得（黑点线），中图接收端网卡按 VM 和类的 wire 速率堆叠，下图虚拟队列。数据：`data/rdmapair_E_20260831_r1_vmclass.csv`（100 ms wire）、`data/rdmapair_E_20260831_r1_flowsets.csv`、`data/rdmapair_E_20260831_r1_goodput.csv`、`data/rdmapair_E_20260831_r1_events.csv`、`data/rdmapair_E_20260831_r1_verdict.csv`。原始数据 `results/rdmapair_E_20260831_r1/`。
