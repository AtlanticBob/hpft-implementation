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
| **控制路径** | **direct-bpf 48µs** | mailbox 13ms | **TCP 270×** |
| **降速数据面响应** | **6–7ms** | ~430ms(水位积分)| **TCP 60×** |
| 高频改速上限 | ~140Hz | ~2–3Hz(430ms 收敛)| **TCP** |
| **租户透明** | ✗(host TC-BPF)| **✓(DPA,host 零改动)** | **RDMA** |

## 结论:两种架构的镜像式权衡

- **RDMA(NIC 硬件 per-QP pacing)赢数据面质量**:空闲/饱和延迟低 20×+、
  latency/bandwidth 天生零耦合(TCP 要 opt3 才勉强追平)、公平严格(spread 0)、
  且**租户透明**。代价是**控制敏捷度差**:receiver-driven 水位积分环 + mailbox,
  降速要 ~430ms、改速上限仅几 Hz。
- **TCP(host 软件 fq+EDT)赢控制敏捷度**:EDT 直接改 departure 时间戳,控制
  48µs、降速 6–7ms、可 140Hz。代价是**数据面质量差**:per-pair EDT 债务把
  latency 与 bandwidth 耦合(饱和小包 23×),要 opt3(gap 旁路)才修好,且修好后
  延迟仍是 RDMA 的 24 倍;**且尚未透明**(在 host 内核)。

架构根因:执行点位置决定一切。RDMA 在 **NIC 硬件**(pacing 精细、并行、透明,但
控制要绕 receiver→mailbox→积分环,慢);TCP 在 **host 内核 qdisc**(改时间戳即时,
但单一 pair 债务串行耦合、且在租户面)。

这也解释了 T3 的方向:把 TCP 也做到 RDMA 那种硬件级数据面质量+透明,需要等价的
硬件/DPA pacing 卸载点——即 T3.2 DOCA/DPDK 例外路径。而若沿用软件 EDT,opt3 已是
该架构下的最优,控制敏捷度反而远胜 RDMA。

## 环境状态

测试后已还原:vf0 cap 回到 6G(31457 units);tx_agent/rx_agent/RP 正常;
host opt3 EDT 仍挂 dpu1vf0。harness:`tools/eval_rdma_shaper.sh`。
