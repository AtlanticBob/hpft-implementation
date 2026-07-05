# TCP shaper 延迟优化(2026-07-05)

评测套件 tools/eval_tcp_shaper.sh 四指标:M1 cap 吞吐(iperf3 @8G,无 RR)、
M2 空闲小包延迟(TCP_RR)、M3 **saturated 小包延迟**(TCP_RR + iperf3 满载)、
M4 saturated 吞吐。判据:每步相对基线是正优化(M3 改善,M1/M2/M4 不回退)。

## 基线与三个优化对比

| 变体 | M1 cap-gp | M2 idle | M3 **SAT-lat** | M4 sat-gp |
|---|---|---|---|---|
| **baseline**(v1 BPF + fq default) | 7.99G | 68µs | **1505µs** | 7.99G |
| opt1 fq flow_limit=16 | 7.99G | 68µs | 979µs (-34%) | 7.99G |
| opt1 fq flow_limit=8 | 7.97G | 81µs↑ | 455µs (-70%) | 7.97G |
| **opt2 小包旁路 EDT** | 7.97G | 64µs | **70µs (-21×)** | 7.98G |
| opt2 + flow_limit16 | 7.98G | 68µs | 67µs | 7.99G |

## opt1:fq flow_limit(粗调,有限)

减小单 flow 在 fq 的排队深度,限制大流量把 pair EDT 债务推远。flow_limit=16
是干净正优化(M3 -34%,无回退);flow_limit=8 更激进(-70%)但 M2 idle 回退
(68→81µs)。缺点:全局参数,边际影响 idle 延迟和吞吐,不精确。

## opt2:BPF 里小包(≤256B)旁路 EDT 延迟 ★ 推荐

`hpft_tcp_edt_kern.c`:小包(RR/ACK/控制,≤256B)不设 skb->tstamp(fq 立即发),
但其字节仍加入 pair 债务(带宽记账)。大包正常 pacing。
- **M3 1505µs → 70µs(21×)**,回到 idle 基线;M1/M2/M4 全部不回退。
- 精确针对 latency 流,不碰吞吐/idle/cap。远优于 opt1。
- 原理:latency-sensitive 流量天然是小包;小包旁路解耦了延迟与带宽,而债务
  记账保证 cap 仍准。这是干净的正优化。

编译:clang -O2 -g -target bpf -c hpft_tcp_edt_kern.c -o .o -I. -I/usr/include/x86_64-linux-gnu
BPF 源与 .o:tcp/bpf/。
