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

## opt3:BPF per-flow EDT 债务 + pair 聚合丢包 ★★ 最通用

`tcp/bpf-opt3/hpft_tcp_edt_kern.c`:
- **per-flow EDT 债务**:5-tuple hash → 独立 next_ns(HASH map,16384 项),
  每个 flow 独立 pace 到 pair 速率。sparse(低带宽)latency 流的 flow 债务
  ≈now → 低延迟,**不依赖包大小**。
- **pair 聚合债务(next_ns 结构不变,兼容 apply)**:累计所有 flow 记账;
  当聚合债务超前 horizon(4ms)则**丢包**(TC_ACT_SHOT)→ TCP 反压,聚合
  维持 cap,但不施加共享延迟(延迟由 per-flow 债务负责)。

标准 eval(小包):M1 7.99G | M2 69µs | M3 65µs | M4 7.99G —— 全不回退,
M3 与 opt2 相当,cap 甚至更准(丢包比小包旁路更精确)。

### 决定性对比:大包(1400B)latency 流 under saturation

| 变体 | 小包 RR sat | **大包(1400B)RR sat** |
|---|---|---|
| baseline | 1505µs | 986µs |
| opt2 小包旁路 | 70µs | **985µs(失效)** |
| **opt3 per-flow+drop** | 65µs | **74µs(13×)** |

**opt2 只对小包有效**(≤256B 旁路);大包 latency 流(大 RPC/大 message)
opt2 失效。**opt3 按 flow 的 sparse/bulk 区分,对任意包大小的 latency 流都
有效**,是更通用、更彻底的正优化。

## 推荐

- **opt3 是首选**:小包+大包 latency 流都保护,cap/吞吐/idle 全不回退。
- opt2 是轻量备选(仅小包场景,BPF 改动更小,无 per-flow map 内存)。
- opt1 flow_limit 可作为不改 BPF 时的快速缓解。
- opt3 生产化 TODO:per-flow HASH map 需老化(LRU_HASH + spin_lock 内核
  当前不支持,需自定义老化或无锁 per-CPU 方案);horizon 参数按链路 RTT 标定。

编译:clang -O2 -g -target bpf -c hpft_tcp_edt_kern.c -o .o -I. -I/usr/include/x86_64-linux-gnu
(helpers.h 需补 bpf_map_update_elem 声明,helper ID 2)。
