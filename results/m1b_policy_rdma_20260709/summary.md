# M1b — 政策瓶颈（单类 RDMA）+ 旧链路退役（方案 E，design_e §5.3）

日期：2026-07-09（UTC）。E 环接管 RDMA 类：tx_agent_e → PCC RP mailbox
（budget = pace_f，rate = 遥测 r_f），旧 tx_agent2/rx_agent 正式停役。
vf0 RDMA（ib_write_bw 110s），政策阶跃 6G → 4G → 6G。

## 验收结论（标准同 M1）

| 标准 | 结果 |
|---|---|
| 稳态 = 目标 ±5% | **通过**：5.842G(−2.6%) / 3.916G@4G(−2.1%) / 5.936G(−1.1%)；中位数 5.97/3.99/5.98；逐 tick 带内 89-92%（显著优于 TCP 的 58-65%——PCC 硬件 pacing 无租户 CC 锯齿） |
| 收敛 <10 周期 | **通过**（下行阶跃 4 周期越过目标）；完全稳定 25 周期（AIMD 恢复尾，同 M1）；上行 18 周期（AI 步长限速，符合设计语义） |

## lossy RDMA 配置（本里程碑前置，2026-07-09 完成）

- PFC：两 DPU p1 全 priority 关闭（原本即关）。
- 802.3x 全局 pause：dpu2 p1 原为 RX/TX on（最后一跳被无损化），已关闭；
  dpu1 原本即关。现在两端全 lossy，无交换机 ECN 依赖，RC 重传兜底。

## RDMA 执行面接线（根治 tx_agent2 两缺陷的结构性方案落地）

- mailbox 协议复用（`0xb47c000N ft bud rate` 批量行，PCC device 码未动）。
- **rate 输入 = 遥测 r_f（按类分流）**：旧 tx_agent2 用 rep rx_bytes
  （TCP+RDMA 总和）喂 RP 积分控制，同 VF 的 TCP 会把 RDMA 压穿地板
  （M0 根因 (b)）；现在 RDMA 速率测量与 TCP 完全隔离。
- 配置全部 registry 驱动（flowtag 映射），消灭手维护 IP/设备表（根因 (a)）。

## 过程中发现并修复的三个问题

1. **RP FIFO reader EOF 死亡**：`rp_service.sh` 的 FIFO 保活写端是
   `sleep 1200`（20 分钟早已过期）；旧 tx_agent2 停役时关闭了最后一个写端 →
   doca_pcc 的 stdin 读到 EOF 后永不再读 → 后续 budget 全部无效（首轮 M1b
   4G cap 不生效的根因；写端缓冲填满 64KB 后写入也开始失败）。修复：
   重启 RP + tx_agent_e **启动即持有 FIFO 写端并常持**（ensure_open，
   inode 变化自动重开），EOF 空窗不再可能。
2. **ceiling 被幽灵兄弟流稀释**（rx_agent）：全无穷需求算 ceiling 时，
   perftest 控制连接（tcp 类，速率≈0）把 rdma 的 ceiling 均分到 3G < r →
   排空公式 (ceil−r) 变负 → vq 反向充入 → s=1 死挂。修复：ceiling 逐流
   计算（该流需求无穷、其他流保持实际需求）+ 排空量 max(·,0) 钳位。
3. **采样抖动尖峰**：偶发慢读（30-45ms）使相邻 tick 间隔缩到 1-20ms，
   vport delta/dt 算出高达 264G 的假速率 → 幽灵 vq 充入 → 假 MD → 真实
   速率被砍出坑。修复：间隔 <30ms 不重采样（delta 滚入下 tick）。

## 数据与 lab 状态

数据：`rx_e.jsonl` / `tx_e.jsonl`（全程逐 tick）、`m1b_client.log`、
时间戳 `m1b_t0/step4/step6/t1.txt`。

lab：旧 hpft-rxagent（dpu2）/hpft-txagent（dpu1）已 stop（未 disable，
可随时拉回）；RP 已重启（预算状态清零，由 E 环按需设置）；E 环三件套
（hpft-rxagent-e / hpft-txagent-e / hpft-pace-shim）为现行控制面。
遗留观察：稳态仍有零星单 tick 尖峰（≤15G，疑似 RC 重传微突发），
不影响均值验收；M4 一并观察。
