# Phase 2 报告:RDMA shaper v2 生产形态(2026-07-04)

## 总判定:五条设计目标全部达成

v2 在 BF3(DOCA 3.4.0112 / fw 32.49.1014)上实现了对租户完全透明的
发送端 RDMA 速率限制,粒度 {src_vnic, dst_vnic},执行点全部在 VF 边界以下
(DPA 硬件 pacing),这是 v1(patched rdma-core,租户可见)无法做到的。

| 设计目标 | v2 达成 | 证据 |
|---|---|---|
| 统一粒度 {src_vnic, dst_vnic} | ✓ | flowtag→src,qpn override→dst;多 dst 6G/2G 独立 |
| 租户零改动零感知 | ✓ | 执行在 DPA;host/租户全程无任何安装或配置 |
| 发送端 pacing 非丢包 | ✓ | RNIC QP scheduler dequeue-rate,非 policing |
| 多 QP/多进程/多 flow 共享 cap | ✓ | 1024 QP 聚合 +0.3%;pair 级预算,与流数无关 |
| 活跃长流高频改速 | ✓ | cap 传播 394ms;10Hz 干净跟踪;JSON-lines 运行时改速 |

## 架构

```
租户 QP 发送(原生 verbs,零改动)
  → RNIC 产生 CC 事件(TX/CNP/RTT)
  → DPA 上的 RP 算法(hpft_rp_main_v2.c):
      · qpn override 表命中 → pair(多 dst);否则 flowtag 扫描(单 dst)
      · 水位积分控制:level 收敛使 Σrate = pair 预算
      · DCQCN-lite:cc_rate 响应 CNP;rate = min(cc_rate, level)
      · 每 QP 硬件 pacing rate = min(cc, level)
  ← 控制输入:
      · 接收端 rx_agent 测 per-dst representor 速率(精确 R,无事件丢失)
      · 批量 mailbox(0xB47C/0xB48D/0xB48E)从 Arm agent 下发 cap + R + qpn 映射
      · hpft-shaper-controller 从 registry 驱动,JSON-lines 运行时改速
```

## 关键工程发现(逐个有实测支撑)

1. **PCC 需固件/DOCA 同代**:fw 32.47 + DOCA 2.9 的 PCC outbox 创建失败
   (syndrome 0x6d0fd5);刷 BFB 3.4 对齐后解锁。
2. **VF 覆盖成立**:PCC 对 host VF 的 RoCE 流量限速有效(9.18G@10G cap),
   host 零改动。5/28 观察到的"VF 不受控"是旧固件栈问题。
3. **flowtag = 流级 hash 含 dst**:同 src VF 同源 IP 到不同 dst 的 flowtag 不同,
   非纯 per-src-function。algo_ctxt 按 flowtag 共享(4 QP→1 ctx),故 CC 状态
   必须自建于全局 pair 表。
4. **R 测量必须是发送端 wire 或接收端 per-dst 口径**:接收端 goodput 在丢包下
   与 wire 脱钩(82% 丢包时 6.9G vs 38.8G wire);发送端 vport 精确但无法分 dst,
   多 dst 用接收端 per-dst representor。
5. **高流数需慢速积分控制**:per-flow 速率应用延迟使快控制律留"陈旧速率质量"
   (+55%);小步积分 + 最小步长(消截断死区)→ 1024QP +0.3%。
6. **min-level 地板**:启动突发压穿 level 会让 64KB 消息时间超 RC 重传窗口 →
   QP 死亡;地板 budget/128 保命。
7. **DPA API 上下文白名单极窄**:packet handler 里调 trace = core dump;
   mailbox 里调 nic counters = fatal RPC;__atomic_exchange_n 编译过但运行时坏。
   只有 per-thread 分片 + 竞态容忍写法可靠。
8. **接收端驱动通道**:RP↔对端 NP 的 CCMAD RTT 负载往返打通(µs 级),但
   CCMAD SW-NP 分发按目的 function 作用域,host VF 目的地结构性不可达
   (ECPF/SF 可达);VF 流量转 Arm-to-Arm agent + mailbox 批量(~15ms 级)。

## 验收数据汇总

| 场景 | 结果 |
|---|---|
| 固定 cap 精度(1–64G,线上) | -1.7% ~ -3.2% |
| 1024 QP 聚合(8G budget) | +0.3% |
| 四 pair 并发(8/4/2/2G) | ≤1.7% |
| 多 dst 单源(6G/2G) | -0.2% / 0% |
| work-conservation | 贪婪流吃满剩余,聚合守 budget |
| cap 变更传播(8G→4G) | 394ms |
| min(cc,level)(冻结注入) | cc=1G→0.97G,恢复→7.76G |
| mailbox 更新 | 单条 75Hz;批量突破 |

## 与 SCR(NSDI'25)对照

v2 复刻并落地了 SCR 的核心命题(dequeue-rate 控制 + DPA 软件控制),并在
其上补齐了工程化路径:pair 级预算(而非 per-QP fair)、registry 驱动的
{src,dst,rate_bps} 控制接口、接收端驱动的精确 R 测量、多 dst 的 qpn override。
SCR 未覆盖的 R 测量口径、启动动力学、DPA 上下文陷阱等在本项目中被逐一解决。

## 遗留(优先级序)

1. 真实 CNP 自动触发验证(需 ECN/incast 环境;handler 已就位,见
   docs/b_dcqcn_cnp_status.md)。
2. 多 src 到同一 dst 的 per-(src,dst) R 测量(当前每 dst 单 src 干净)。
3. 控制面进程的生产级 systemd 托管(当前 keepalive wrapper)。
4. device 代码结构性重构(当前功能正确但为增量 patch 累积)。
5. TCP shaper v2(下一阶段)。
