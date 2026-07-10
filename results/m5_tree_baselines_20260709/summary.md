# M5 — 发送端树 + 仲裁器 + baseline 对比（方案 E，design_e §5.3）

日期：2026-07-09（UTC）。

## M5a — 发送端树（§3.6，替换静态 Tree 桩）

tx_agent_e 新增 SenderTree：与接收端同构的三层 water-filling（src VM
MaxRate → 类权重 → per-fs），容量 = 上行线速×(1−headroom)，需求 =
遥测 r_f×(1+δ) + cap-hit 加成（r ≥ 0.85×pace 时按 pace×(1+δ) 报需求）。
每条遥测消息重算（20Hz），用于 R 初值（Q20）、AI 上限、fail-open 目标、
pace=min(R_f, Tree_f)。**类间借用/预算仲裁器 = 树的 work-conserving 填充
本身**——两个执行面（fq+edt / PCC）挂在同一 agent 下，无需独立仲裁进程。

**min 不变式实测**：发送端 vf0 MaxRate=5G（比 dst 侧 6G 更紧）→ Tree=4.95G
成为 binding 约束，RDMA 实测 4.54G（30s 含爬坡；tx 日志确认 tree=4.95G
生效）。两个标记源（dst 侧 marks + 本地树）共用一条律，谁紧谁生效 ✓。

⚠️ **设计问题记录（待用户裁决）**：§3.6 原文的树是"需求封顶"填充，但
Q20（新流以 Tree_f 起步获"全部本地额度"）与 §3.5（fail-open 斜坡回升至
Tree_f）都要求 Tree_f 是"政策上限"——需求封顶下新流 r=0 ⇒ Tree≈0，自相
矛盾。实现采用：**Tree_f = 该流需求无穷、兄弟流实际需求下的公平上限**
（与接收端 ceiling 同构）。语义自洽且三处引用全部满足。

## M5b — baseline 对比（论文主图数据）

场景同 M2：vf0 对 TCP+RDMA，租户政策 6G、类权重 1:1，观察双活分配与
TCP 空闲时的借用。

| | B0 无隔离 | B1 静态 cap（3G+3G） | **E** |
|---|---|---|---|
| 双活分配 | tcp **36.9G** / rdma **147.1G**（政策完全失效，比例=CC 碰撞副产品） | 2.98 / 2.93 | 2.61 / 2.52（1.03:1，可在线改 3:1→2.80:1） |
| TCP 空闲时 RDMA | —（本就无约束） | **2.94G（份额浪费 3.1G/6G）** | **5.89G（借用 98%）** |
| 权重可编程 | 无 | 人工改静态值 | 政策热加载，回收 2 周期 |

B0/B1 数据 `b0_*` / `b1_*`（rx meter 逐 tick + 自报）；E 数据引 M2。
方案 D 租约版 ablation（可选项）未做，留后续。

## 事故记录（配置漂移）

M4 收尾的 registry scp 把 dpu2 上的 vf0 MaxRate 覆盖回 null（repo 副本
未同步 M3 的恢复性 ssh 直改）→ M5 验证一度出现"cap 不生效"（rx 显示
e=164G/s=0 即刻定位）。教训已固化：**repo config/lab-registry.json 为
唯一真值，一切政策改动先改 repo 再下发**；实验中的临时 ssh 直改必须在
实验尾同步回 repo。已按此恢复并复测（5.66G @6G ✓）。

## 结束时 lab 状态

E 三件套运行中（推荐参数 β=0.3、V=2T、A=0.1G 全局折中、m_al=10）；
政策 vf0 MaxRate=6G 类权 1:1，其余 VM 无 cap；p1 双端 200G；lossy
（PFC/pause 全关）；旧链路（hpft-rxagent/hpft-txagent）stop 未 disable；
RP 带 sleep-infinity FIFO 保活。
