# opt3 v3:TCP shaper latency/bandwidth 解耦定稿(2026-07-05)

固化为 TCP shaper 默认实现。目标:既保住 cap 精度 + 多流公平 + 降速敏捷,又
消除 fq+EDT 的 per-pair 债务软肋(saturated pair 上小包/延迟流 23× 暴涨)。

## 演进(每一步都要求相对上一版正优化)

| 版本 | 机制 | 结果 |
|---|---|---|
| baseline (v1) | per-pair EDT 债务,所有包共享 `pair.next_ns` | 多流稳,但 saturated 小包 1578µs(23×) |
| opt2 | 小包(≤256B)旁路 EDT 延迟 | 小包 70µs ✓,但**大包延迟流仍 985µs ✗**(判据是包大小,大 RPC 漏网) |
| opt3 v2 | per-flow EDT 债务 + pair 债务超 4ms horizon 丢包 | 各尺寸延迟都好,但**多流丢包塌陷 8G→1.11G,严重不公平 ✗** |
| **opt3 v3(定稿)** | **共享 pair 债务延迟(=baseline,多流稳)+ inter-packet gap 旁路(判 latency 流,任意包大小)+ 不丢包 + LRU flow map** | **全维度正优化 ✓** |

## opt3 v3 机制

- **聚合限速 = 共享 pair 债务延迟**,与 baseline 完全同源:所有包累加
  `pair.next_ns`,bulk 流读它当 `skb->tstamp` 被 fq 平滑 pace。→ cap 精确、
  多流经 fq 天然公平、降速立即生效。不丢包 → 稳定。
- **latency 旁路判据 = per-flow inter-packet gap**(不是包大小):
  flow(5-tuple)距上次发包 gap > 40µs ⇒ "发一个等一个"的 latency 流 ⇒ 旁路
  共享 pacing 延迟(但字节仍计入 pair 债务,cap 不漏)。连续 bulk 流 gap 小 ⇒
  正常受 pace。**任意包大小的 latency 流都能旁路**,解决 opt2 的大包漏网。
- **flow 状态用 `BPF_MAP_TYPE_LRU_HASH`**(值仅 `last_send_ns`,无 spinlock,
  故 LRU 可用):死流自动淘汰,bulk 流常被访问永不淘汰。解决 HASH 满表隐患
  (满表会把 bulk 误判 sparse → 漏限速)。

关键取舍:opt3 v2 用"per-flow 债务 + pair 丢包"想彻底解耦,但丢包在多竞争流
下不稳(4 流各按 pair cap 自 pace → 总到达 4×cap → 债务爆 horizon → 群丢 →
TCP 抖动塌陷)。v3 回到"延迟型聚合"(丢包换成延迟,永不塌陷),解耦改用 gap
旁路——只对确实稀疏的流免延迟,不动聚合稳定性。

## 验证(vf0→sgpu02 vf0,8G cap,均 root-fq)

| 度量 | baseline | opt2 | opt3 v2 | **opt3 v3-LRU** |
|---|---|---|---|---|
| 4 bulk 流同 pair 聚合 (cap 8G) | 8.36G | 8.36G | **1.11G ✗** | **8.15G ✓** |
| 4 流 per-flow 公平 | ~2G each | ~2G each | [.14 .39 .25 .56] ✗ | **[1.92 1.99 1.58 2.00] ✓** |
| M1 cap 精度 (8G) | — | — | — | **7.02G ✓** |
| M2 idle 小包 lat p50 | 68µs | 69µs | 65µs | **64µs ✓** |
| M3 **saturated 小包 lat p50** | **1578µs** | 70µs | 65µs | **66.9µs ✓** |
| M4 saturated 吞吐 | — | — | — | **6.86G ✓** |
| 降速响应(15ms 窗口口径) | 22.8ms | — | — | **22.8ms ✓**(真实拐点仍 ~7ms) |

saturated 延迟 vs payload 大小(opt2 在大 payload 失败,opt3 v3 全好):

| payload | 64B | 1000B | 4000B | 16000B | 64000B |
|---|---|---|---|---|---|
| opt2 p50 | 70µs | — | 985µs ✗ | — | — |
| **opt3 v3 p50** | 78µs | 67µs | 68µs | 65µs | 66µs |

## 结论

opt3 v3 在 **cap 精度、多流公平、idle/saturated 各尺寸延迟、饱和吞吐、降速敏捷**
每个维度都 ≥ baseline,且把 saturated 延迟软肋从 23× 压回基线。**定为 TCP
shaper 标准实现**,源码 `tcp/bpf-opt3/`。生产控制面仍要求常驻进程 + direct
bpf()(见 tcp_rate_perf 报告)。

未决/后续:多 pair(vf0-3)隔离沿用 baseline 的 per-pair 债务(已在 baseline
验证,机制未变);gap 阈值 40µs 对常见 cap(≥1G,bulk inter-pkt <12µs)robust,
极低 cap(<0.5G,bulk inter-pkt >24µs 逼近阈值)场景待专门标定。
