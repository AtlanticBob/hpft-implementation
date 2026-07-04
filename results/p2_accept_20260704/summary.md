# P2-1/P2-3 验收:接收端驱动 agent 架构下的 1024QP 精度(2026-07-04)

## 最终架构(全部实测验证)

接收端 agent(dpu2, systemd):cap 策略 → UDP →
发送端 agent(dpu1, systemd):本地 representor vport_rx_bytes 测发送 wire 速率(20Hz)
→ 批量 mailbox(0xB47C|n,一次 RPC N 个 pair)→ RP pair 表 {budget, rx_rate}
→ 水位积分控制(每新鲜样本一步,±lvl/8 限幅,消截断死区)→ per-QP 硬件 pacing。

## 稳态精度(8G 预算,15-20s 窗口线上计数)

q=1: -3.0%(协议头口径);q=64: -0.1%;q=256: -0.1%;**q=1024: +0.3%**。
修复链:+428%(接收端 goodput 语义错位,丢包下与 wire 脱钩)
→ +55%(发送端本地 vport R,语义正确)
→ +12.7%(积分控制,残余为整数截断死区)
→ **±0.3%**(最小步长消死区)。

## 关键工程结论

1. R 测量必须是发送端 wire 口径:接收端 goodput 在丢包状态与 wire 脱钩
   (实测 82% 丢包时 goodput 6.9G vs wire 38.8G);cap 语义(接收端策略)与
   R 测量(发送端本地)分离后两全。
2. 高流数下 per-flow 速率应用延迟(事件节奏)要求 level 慢速单调收敛
   (小步积分),快速 bang-bang 律必然留下陈旧速率质量(+55% 的来源)。
3. 整数控制律的截断死区随 1/level 放大(1024 流时 ~19%),必须显式最小步长。
4. DPU 上常驻进程一律 systemd-run 托管(Restart=always);
   ssh+setsid+nohup 链在跳板下不可靠(多次静默未启动)。
5. tx_agent 的 FIFO 写必须按 inode 检测重开 + O_NONBLOCK(RP 重启重建 FIFO)。

## 待补验收(机制同 S5 已证,形式复测)

work-conservation(需求受限流+贪婪流)、双 pair 独立 cap、cap 变更传播延迟。

## 复测 A(双 pair)未通过——遗留 bug 现场(2026-07-04 晚)

vf0(8G)单独与并发均精确(-0.1%);**vf1(4G)流在数秒内冻结死亡**:
- 逐秒轨迹:remote_rx 在活跃流量中间歇读 0(t1=0, t2=14184, t3=0...),
  level 随之被误压(7601→5309→4523),QP 疑似重传耗尽后连接死亡
  (perftest 挂起无结果,epoch 计数静止);
- vf0 同构路径无此现象 → 怀疑点:tx_agent2 对 pf1vf1 的 ethtool 采样间歇失败/
  超时被跳过、双设备采样超 50ms tick 预算、或 vf1 flowtag 在刷机后的稳定性;
- 后续排查入口:tx_agent2 加 per-tick 日志(v, dt, rate)对照 0xdeb 轨迹;
  控制器加"rx=0 但事件活跃则不收缩"保护;RoCE retry 死亡可由 min-level
  地板(如 bud/1024)预防。

## 复测全通过(2026-07-04 第二轮)+ vf1 bug 根因闭环

**vf1 bug 根因**(非控制器、非计数器、非路径):流启动瞬间的线速突发把
level 按积分律压到 MIN(0.38Mbps/QP),8QP×64KB 消息传输时间 1.4s 超过
RC 重传窗口 → QP 死亡;且被压碎的 level 跨测试残留在 pair 里(预算不变
不重置),毒化后续所有 vf1 实验形成"vf1 特有"假象。判别链:vf1/2/3 裸跑
185G 全好 → 互换预算双向存活(预算变更重置 level)→ 压碎-残留机制确认。
**修复:min-level 地板 = budget/128 + MIN**(4G 预算下 31Mbps,消息 17ms,
QP 永不死),加上后 vf1@4G 连续通过。

| 复测 | 结果 |
|---|---|
| A 双 pair 并发(vf0 8G ∥ vf1 4G) | 7.99 / 4.00(各 -0.1%) |
| B work-conservation(受限 1.51G + 贪婪) | 贪婪 6.05G 吃满剩余,聚合 7.99G |
| C cap 8G→4G 传播(单时钟测量) | 394ms 到稳定 4.01G(链路各 tick 之和) |

附:跨机时钟做延迟测量不可行(date -s 同步后数小时漂移已达秒级),
单时钟方案(采样与改档同机)是标准做法;DPU 应尽快配 NTP/chrony。
