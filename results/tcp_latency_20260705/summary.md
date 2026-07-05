# TCP 包延迟 vs shaper 控制延迟,及 shaping 对延迟的影响(2026-07-05)

方法:TCP_RR(1 字节请求/响应,TCP_NODELAY 关 Nagle),5000 次,测往返延迟
分布。vf0(10.1.0.1)→ sgpu02 vf0(10.1.0.2),经 BF3 eSwitch。

## 1. TCP 包延迟基线(无 shaping)

| | 往返 |
|---|---|
| min | 60–64 µs |
| **p50** | **68 µs** |
| p90 | 73–75 µs |
| p99 | 82–91 µs |
| 单程 | ~34 µs |

## 2. 控制延迟 vs 包延迟对比

| 量 | 值 | 相对 1 个 RTT(68 µs) |
|---|---|---|
| 控制路径 direct bpf p50 | 48 µs | 0.7 RTT(1 个往返内完成) |
| 控制路径 direct bpf p99 | 340 µs | 5 RTT |
| 降速数据面响应 | 6–7 ms | ~100 RTT |

**结论:控制延迟不是瓶颈**。48 µs 的单次更新比一个 TCP 往返还短;即便 p99
340 µs 的尾延迟,也完全淹没在数据面降速响应 6–7 ms 里(占 <5%)。真正的时间
常数是数据面 6–7 ms,控制路径快一到两个数量级。用户担心的"p99 近 ms"在数据面
6–7 ms 面前无关紧要。

## 3. shaping 对 TCP 包延迟的影响(关键)

| 场景 | TCP_RR p50 |
|---|---|
| 无 shaping | 68 µs |
| shaping ON,pair **空闲** | 69 µs(无影响 ✓) |
| shaping ON,pair 被 8G 大流量**占满**(默认 fq) | **1578 µs(暴涨 23×)** |

**这是 fq+EDT 的真实软肋**:latency-sensitive 小包与 bandwidth-hungry 大流量
在同一 pair 时,小包延迟从 68 µs 涨到 1.58 ms。根因:BPF 给同 pair 所有包设
`skb->tstamp = pair.next_ns`,大流量把 pair 债务(next_ns)推到 ~1.5 ms 后,
小包继承这个债务、被 fq 一起延后。这正是 SCR 论文讲的 latency/bandwidth 矛盾。

## 4. 优化(已验证,几乎免费)

降低 fq 的 `flow_limit`(单 flow 在 fq 的排队深度上限),限制大流量把 pair
债务推远的程度:

| fq 配置 | saturated 小包 p50 | iperf3 goodput(8G cap) |
|---|---|---|
| 默认 flow_limit=100 | 1538 µs | 7.65 G |
| flow_limit=10 | 67 µs | 7.64 G |
| flow_limit=5 | 68 µs | 7.62 G |

**flow_limit 100→5:延迟 23× 改善(回到基线 68 µs),吞吐几乎无损(7.65→7.62 G)**。
机制:浅 fq 队列 + TCP small queues(TSQ)反压,让大流量不在 fq 深排队,
pair 债务不积累,小包及时发送。这是**立即可用的优化**——只需 tcp-shaper-apply
在建 fq 时加 `flow_limit 8` 之类。

## 5. 更好的实现思路(排序)

1. **fq flow_limit 调优(立即,免费)**:如上,把 saturated 小包延迟压回基线。
   建议 shaper 默认设 `flow_limit 8`。
2. **BPF 里给 EDT 债务加 horizon 上限(对症,小改)**:不让 `pair.next_ns`
   超过 `now + H`(如 H=200 µs),直接把小包最坏延迟钳到 H。比 flow_limit 更
   精确可控,且不依赖 TSQ 行为。这是最推荐的针对性优化。
3. **per-flow EDT 债务 + pair 级预算聚合(架构级,借鉴 RDMA v2)**:当前债务是
   per-pair,小流量继承大流量债务。改成每 flow 独立 EDT、pair 总 cap 用令牌桶/
   水位(正是 RDMA v2 的 per-QP pacing + pair 预算)。彻底解耦 latency 与
   bandwidth 流,但要重写 BPF。
4. **优先级/DSCP 感知旁路**:latency 流(小包/稀疏或带 DSCP)走独立债务或高
   优先级队列。需要分类启发式或租户配合(后者不透明)。
5. **DPU 硬件 QoS(卸载路线)**:eSwitch 优先级队列 + per-flow rate limit,
   latency 流走高优先级。彻底但需用户态数据面卸载(另一条大路线)。

## 总结

- TCP 包 RTT 68 µs;控制路径 48 µs 更快,不是瓶颈;数据面降速 6–7 ms 才是时间
  常数。控制延迟的绝对值相对包延迟完全够好。
- **shaping 对延迟的真实影响在 saturated pair 的小包(23× 暴涨)**,这是 fq+EDT
  per-pair 债务的固有问题。
- 已验证 flow_limit 调优能几乎免费消除它;更彻底的是 BPF EDT 债务 horizon 上限
  或 per-flow 债务(RDMA v2 式)。这些都是软件层可做的优化,无需换 shaper 架构。
