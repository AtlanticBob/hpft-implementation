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
