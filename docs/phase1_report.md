# Phase 1 探测报告:DOCA PCC 作为 RDMA shaper v2 执行点(2026-07-03)

## 总判定:**GO**

在 BF3(DOCA 3.4.0112 / fw 32.49.1014)上,DPA PCC 满足 v2 的全部核心
设计目标:租户零改动零感知、发送端 pacing(非丢包)、PF 与 **VF** 均受控、
同 pair 多 QP/多进程共享总 cap、活跃流跟踪高频限速变更、fail-open。

## Q1–Q4 答案

**Q1 VF 覆盖(go/no-go)= GO。** 固定 10G 算法钳制 PF 至 9.73G、VF 至
9.18G(goodput),退出后恢复线速;host 侧全程零改动。用户提醒的 5/28
"VF 不受控"是旧固件栈(fw 32.47 + DOCA 2.9,PCC 整体损坏前的另一表象)
的问题,新栈不存在。前置条件:**固件与 DOCA 用户态必须同代**
(经历了完整的错配诊断与 BFB 3.4 刷机,见 s1_blocker 文档)。

**Q2 流标识 = 基本可用,dst 需带外映射。**
- `flowtag` = 源 function(vNIC)稳定标识(跨运行/进程/QP 不变)→
  {flowtag → src_vnic} 表即为 src 侧映射;
- `flow_qpn` = 发送端本地 QPN(与 host 可观测的 QPN 精确一致);
- `port_num` 区分物理口;
- dst 不在事件中 → Phase 2 由 Arm/host agent 维护 {(src_vnic, qpn) → dst_vnic}
  (provider 域内可得,不触碰租户),mailbox 下发;
- 注意:硬件 algo_ctxt 按 flowtag 共享,per-QP/per-pair 状态必须自建于
  DPA 全局内存。

**Q3 控制路径 = 10 Hz 从容,100 Hz 需批量。**
- mailbox 同步 RPC ≈ 13.2 ms(与 handler 内容无关)→ 单通道 75.3 Hz 封顶;
- 生效延迟上界 13 ms(回调先于返回完成);
- 精度:线上口径 1–64G 全部 -1.7%~-3.2%(≤5% 门通过);goodput 口径另有
  ~8% 协议头系数,标定即可;
- 10 Hz 方波:活跃流双峰干净跟踪(3.92/7.71G),无粘滞;
- Phase 2:一次 mailbox 携带 N 个 {pair, cap} 批量更新以达 100 Hz 等效。

**Q4 共享 cap = 成立。** cap/N 均分:1/2/4 进程、1/4/16 QP 聚合误差
<0.3%;双 pair 独立 cap 并发互不干扰;fail-open/删除恢复全部通过。
边界:16 槽流注册表超订(64 QP)时按比例超发 → Phase 2 换 pair 级
令牌桶/预算制(DPA 原子操作)+ water-filling。

**Q0 安全回滚**:doca_pcc 退出(SIGINT/超时/kill)即恢复固件 CC,
全天多次启停无残留;运行期间整卡 CC 被替换(实验纪律不变)。

## 关键量化数据

| 项 | 值 |
|---|---|
| 基线(刷机后) | PF 196.03 G;VF ~183 G(MTU 变小所致,已确认非问题);写延迟 2.40 µs(刷机前) |
| 固定 cap 精度(线上) | -1.7% ~ -3.2%(1–64G) |
| mailbox RPC | p50 13.24 ms,p99 14.72 ms;持续 75.3 Hz |
| 10 Hz 跟踪 | 10.03 Hz,双峰 3.92/7.71 G |
| 共享 cap 聚合误差 | <0.3%(≤16 流) |

## Phase 2 设计要点(从探测直接导出)

1. 生产算法 = 参考 DCQCN(rtt_template)+ `rate = min(cc_rate, pair_budget)`;
   pair 预算用 DPA 全局令牌桶,不用 cap/N 均分。
2. pair 键:{flowtag→src_vnic}(DPA 内)× {qpn→dst_vnic}(host agent 带外,
   mailbox 下发;host `rdma res` 可见 QPN/对端,验证于 Phase 2)。
3. controller 批量更新协议:单 mailbox 消息打包 N 个 {pair_id, cap}
   (request buffer 可配大);100 Hz 等效更新。
4. 流记账:哈希表 + 老化,替代 16 槽线性表。
5. 已知遗留:DPU NTP(时钟落后 ~88226s);
   goodput/线上口径标定;1024 QP 压测(v1 验收线)在预算制实现后做。

## 过程记录索引

- 刷机与解锁:`docs/s1_blocker_pcc_fw_mismatch.md`、`backup/dpu-pre-reflash-20260703/`
- 各步结果:`results/s0_baseline_20260703/`、`results/s1_s2_pcc_20260703/`、
  `results/s3_flow_identity_20260703/`、`results/s4_rate_update_20260703/`、
  `results/s5_shared_cap_20260703/`
- 探测代码快照:`pcc/s2-fixed-rate/`(S2/S3/S4 各版本 device main + host 补丁);
  DPU 上活树:`~/bzx/doca34-apps`(S5 版本)
