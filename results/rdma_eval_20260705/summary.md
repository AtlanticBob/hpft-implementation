# RDMA shaper 同法测试 + RDMA vs TCP 对比(2026-07-05)

用与 TCP 完全相同的度量测 RDMA shaper(PCC on DPA):M1 cap 精度、M2 空闲
延迟、M3 **饱和延迟(latency/bandwidth 耦合,关键)**、M4 饱和吞吐、降速响应、
多 QP 公平。工具:`ib_write_bw`/`ib_write_lat`(2 字节小消息);harness
`tools/eval_rdma_shaper.sh`。vf0 = mlx5_6 → 10.1.0.2,cap 经 receiver caps 文件
(水位控制 receiver-driven)。

## RDMA 实测(cap 8G)

| 度量 | 值 |
|---|---|
| R-M1 cap 精度 | 7.34G(1 QP)/ 7.56G(4 QP);-5~8% |
| R-M2 空闲小消息延迟 | typ **2.77µs** / p99 3.47 / p99.9 11.3 |
| **R-M3 饱和小消息延迟** | typ **2.79µs** / p99 3.97 / p99.9 10.9 |
| R-M4 饱和吞吐 | 7.34G(latency 测试期间全保留)|
| 降速响应 15G→5G | wire rate 平滑衰减,**~430ms 到位**(水位积分收敛)|
| 4 QP 同 pair 公平 | **[1.89 1.89 1.89 1.89]G,spread 0.00G**,聚合 7.56G |

**关键**:R-M3 饱和延迟 2.79µs ≈ R-M2 空闲 2.77µs,**latency/bandwidth 零耦合**。
bulk QP 打满 pair 时,同 pair 的小消息 latency QP 完全不受影响。根因:RDMA 用
**per-QP 硬件 pacing**(rate=min(cc,level)),每 QP 独立 pacer;不存在 TCP
per-pair EDT 那种"共享 next-departure 时间戳被大流推远"的耦合。

## RDMA vs TCP 逐项对比

| 度量 | TCP(fq+EDT opt3,host) | RDMA(PCC/DPA) | 赢家 |
|---|---|---|---|
| cap 精度(8G) | 7.02G | 7.34G | 平 |
| 空闲小消息延迟 | 64µs(TCP_RR)| **2.77µs**(ib_write_lat)| **RDMA 23×** |
| **饱和小消息延迟** | 未优化 1578µs(23×耦合)→ opt3 66µs | **2.79µs(零耦合,无需修)** | **RDMA 24×**(且天生) |
| latency/bandwidth 解耦 | 需 opt3 gap 旁路才做到 | **架构天生**(per-QP pacing) | **RDMA** |
| 多流/QP 公平 | opt3 [1.92 1.99 1.58 2.00] spread 0.4G | **[1.89×4] spread 0.00G** | **RDMA** |
| 饱和吞吐 | 6.86G | 7.34G | 平 |
| **控制路径** | **direct-bpf 48µs** | mailbox sub-ms | 平 |
| **降速数据面响应** | 6–7ms | ~430ms → **~5-7ms(已优化)** | **平**(见下) |
| 高频改速上限 | ~140Hz | ~2–3Hz → **~100Hz+(已优化)** | 平 |

> **更新(2026-07-05,rdma_ratechange_20260705)**:降速 430ms 已优化到 **~5-7ms**
> (比例前馈 + settle-hold + faster agents),与 TCP 6-7ms 持平。原"TCP 控制敏捷度
> 碾压 RDMA"的结论已被推翻——瓶颈是 DPA 积分控制律 + agent 采样率,非硬件/信道,
> 软件层修好。下表"控制敏捷度"列的 RDMA 劣势已消除。
| **租户透明** | ✗(host TC-BPF)| **✓(DPA,host 零改动)** | **RDMA** |

## 结论(2026-07-05 降速优化后)

降速优化前是"镜像式权衡"(RDMA 赢数据面、TCP 赢控制敏捷度)。**优化后 RDMA 补齐了
控制敏捷度**(降速 430ms→~5-7ms、改速 ~100Hz+),于是:

- **RDMA(PCC/DPA)现在几乎全面领先**:数据面质量(延迟低 20×+、latency/bandwidth
  天生零耦合、公平 spread 0)、租户透明、且控制敏捷度已追平 TCP(降速 ~5-7ms、
  控制路径 sub-ms)。唯一还需注意:连续高频改速的上限受 mailbox 串行处理(~100Hz);
  若未来要更高,P2-3 的 SF NP 代理仍是备选路。
- **TCP(host 软件 fq+EDT)** 仅在"纯软件、无需 DPU/DPA"这一点上更易部署;但数据面
  质量需 opt3 才勉强追平(饱和延迟仍是 RDMA 的 24 倍),且**尚未租户透明**(在 host
  内核)。

架构根因仍成立:执行点位置决定一切。RDMA 在 **NIC 硬件**(pacing 精细、并行、透明);
TCP 在 **host 内核 qdisc**(改时间戳即时但 pair 债务串行耦合、且在租户面)。RDMA 原
先"控制慢"是**软件控制律**问题(积分慢收敛 + agent 采样),已修复——不是硬件短板。

对 T3 的启示不变:把 TCP 做到 RDMA 那种硬件级数据面质量 + 透明,需等价的硬件/DPA
pacing 卸载点(T3.2 DOCA/DPDK 例外路径);沿用软件 EDT 则 opt3 已是该架构最优。

## 环境状态

测试后已还原:vf0 cap 回到 6G(31457 units);tx_agent/rx_agent/RP 正常;
host opt3 EDT 仍挂 dpu1vf0。harness:`tools/eval_rdma_shaper.sh`。
