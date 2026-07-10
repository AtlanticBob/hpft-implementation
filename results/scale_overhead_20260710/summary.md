# 规模/overhead 深挖（2026-07-10）

阶梯：2 → 8 → 20 并发流集合（RDMA 硬件档 2.5G ×4 + TCP 300M ×16，
含 12 条跨 VF 对流；跨对需 per-source 路由 + arp_ignore=1 + rp_filter=2，
已在两 host 配置，保留）。agent 已插桩（rx 分段 µs、tx 树重算 µs）。

## 实测开销（T=50ms 预算）

| 并发 fs | rx sched p50 | rx tick 总 p50/p95 | tx tree p50 | rx CPU | tx CPU |
|---|---|---|---|---|---|
| 2 | 123µs | 1.3/1.5ms | ~95µs | 2.0% | 1.3% |
| 5-8 | 428µs | 1.7/1.9ms | 371µs | 2.9% | 2.7% |
| 17-20 | 1892µs | 3.4/3.7ms | 2043µs | 6.3% | 8.9% |

- 20 fs 时整 tick 3.4ms ≪ 16.7ms 预算，**当前规模无压力**。
- 增长曲线 ~O(N^1.8-2)（per-fs ceiling/tree = N 次独立 waterfill）。
  外推：**~50-60 fs 是现实现的舒适上限**；N=100 时 sched ~45ms 顶穿周期，
  tx 单核 Python 100%。
- read_ms（megaflow dump+解析）1.1-1.3ms 基本平坦——不是瓶颈。

## 发现的优化空间（按收益排序）

1. **单遍 ceiling 算法（最大头）**：ceiling/Tree 现为每 fs 一次独立
   waterfill（O(N²)）。每层其实一遍填充即可推出所有成员的"吸收本层
   剩余后的上限"（份额+剩余量簿记），O(N log N)。预计 20 fs 处 10-20×，
   规模越大收益越大。rx 与 tx 同一套代码可共用。
2. **tx 日志写放大**：每 fs 每 tick 一行（20 fs 时 340 行/s）。生产改
   采样/聚合（间隔或变化触发）。
3. **控制面语言**：Python 解释开销主导（单 fill ~100µs 级）；C/Rust
   重写预计 ~50×，把上限推到 10³ fs 量级。
4. **（性能向，另案）驻留欠账**：A 复扫发现双类利用率 ~85% 与 A 无关
   ——AIMD 标记均衡的结构性驻留欠账（s*>0 常挂）。消除它（如均衡时
   排空補偿）可能一次拿回 10+pt 利用率，属律动力学优化，需谨慎论证。

原始数据：sc_rx.jsonl / sc_tx.jsonl / cpu_samples.txt / sc_rdma*.log；
runner scale_run.sh（注意 perftest --rate_limit 是硬件档位制：2.5/5/10...）。
