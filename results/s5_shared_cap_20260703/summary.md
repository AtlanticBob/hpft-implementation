# S5 结果:per-flowtag 共享 cap(2026-07-03)

实现:DPA 全局 cap 表(8 项,mailbox 8B 消息 {flowtag, cap} 增删)+ 每项
16 槽活跃流注册表(qpn + last-seen,100ms 新鲜窗口),每事件
`rate = cap / N活跃流`;无表项的 flowtag **fail-open**(MAX_RATE)。
vf0 tag=0x74249a41,vf1 tag=0x11f4386b(S3 测得)。cap 单位 2^20=线速 200G。

## 矩阵(goodput 口径;8G 线上 cap 的 goodput 期望 ≈ 7.34)

| 测试 | 结果 | 判定 |
|---|---|---|
| T0 无 cap(fail-open) | 185.13 G | ✓ |
| T1 1 进程 × 1 QP | 7.34 G | ✓ |
| T2 1 进程 × 4 QP | 7.33 G(共享,非 4×) | ✓ |
| T3 2 进程 × 1 QP | 7.32 G | ✓ |
| T4 4 进程 × 1 QP | 7.32 G | ✓ |
| T5 vf0 限 8G ∥ vf1 未限(并发) | 7.34 ∥ 175.88 G | ✓ 隔离+fail-open |
| T6 vf0 8G ∥ vf1 4G(并发) | 7.34 ∥ 3.66 G | ✓ 独立 cap |
| T7 删除 cap | 185.14 G | ✓ 恢复 fail-open |
| T8 16 QP(=注册表容量) | 7.33 G | ✓ |
| T9 64 QP(超出容量) | 28.22 G ≈ 4×cap | 已知边界 |

聚合误差(T1–T4,≤16 流)< 0.3%,远优于 ≤10% 验收门。

## 结论与 Phase 2 输入

1. **同 pair 多 QP/多进程共享一个总 cap 成立**(cap/N 均分策略);
   不同 flowtag 独立、互不干扰;fail-open 语义成立。
2. T9 边界:16 槽注册表下 N 饱和 → 超发 = QP数/16 × cap。生产设计需要
   真正的流记账:更大哈希表,或改为 **pair 级令牌桶/预算制**(DPA 全局
   内存 + 原子操作),避免 N 估计问题;也可结合 water-filling(SCR §6.2)。
3. cap/N 均分意味着各 QP 等份而非按需(欠载流浪费份额);water-filling
   或预算制留给 Phase 2。
