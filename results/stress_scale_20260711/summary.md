# 压测 D1：规模（2026-07-11）

压测第一维。问题：把流集合数量从日常的 2-8 个顶到 4×4×2 全开，1ms 控制面
会不会崩、公平性精度守不守得住、墙在哪。结论先行：**调度器本身没有墙
（线性开销，28 fs 只占单核 ~100%，越界后周期温和拉长），但这轮压测在
执行面挖出了三个此前不可见的真缺陷**——RDMA flowtag 哈希碰撞（平台性质）、
TCP 执行面对跨对流量的两层覆盖缺口（修复）、RP 与响应律之间的跨层活锁
（修复）。修完之后 28 fs 竞争下公平性成立。

## 一、前置工程与第一个发现：flowtag 碰撞

4×4 网格需要跨 VF 对的流量。TCP 侧靠 per-source 策略路由（上次 scale
实验配过但已从 host 上消失——运行时配置漂移，这次写成
`tools/cross_pair_net.sh` 入库，apply/revert/status 幂等）。RDMA 侧此前
控制面只认 4 个直连对的 flowtag：tx_agent_e 按 src 下发预算，而 P2-2
早已证明 flowtag 是 per-{src,dst} 的哈希。本轮用 0xdea 内窥（跑一条无
预算的流、读 RP 记下的未知 tag）把 16 个 (src,dst) tag 全部探明，存进
registry 的 `rdma_flowtags` 表，tx_agent_e 的 RDMA 键改为 (src>dst)、
查不到回退 src（直连对零回归，实测 18.28G 持平）。

探测中发现：**src=vf1 和 src=vf2 对所有 dst 产生完全相同的 flowtag**
（干净复验：逐对独跑、每对前重启 RP 清表、基线读 0x0）。16 个 pair 只有
12 个可区分键，vf1→X 与 vf2→X 的 RDMA 预算在 RP 执行面不可分。tag 与
IP 无关（vf0→vf1 走 10.1.0.4 和 10.1.1.2 同 tag）、跨刷机稳定，是 NIC
固件哈希的性质。历史 M 系列不受影响（直连四对恰好互异）。经用户确认，
本轮用 12 对无碰撞集（含全部直连对，每个 dst 恰 3 个 RDMA 源）+ 16 TCP
= 28 fs；根治路线是 P2-2 已验证的 qpn-override（0xB48D/0xB48E），挂起。

## 二、batch A：开销阶梯（固定负载，只变 N）

阶梯 0→2→8→20→28 fs（RDMA 2.5G 档 ×12、TCP 300M ×16），每级 ~60s，
period_ms=1。结果（sc_rx.jsonl / cpu_samples.txt / analyze_ladder.py）：

| 级 | fs | dt p50/p95/max (ms) | megaflow dump (ms) | tx tree p50 (µs) | rx CPU | tx CPU |
|---|---|---|---|---|---|---|
| 0 | 0 | 1.000/1.000/1.000 | 0.95 | – | – | – |
| 1 | 2 | 1.000/1.000/1.000 | 1.18 | 102 | 44% | 38% |
| 2 | 8 | 1.000/1.000/1.000 | 1.53 | 230 | 68% | 85% |
| 3 | 20 | 1.000/1.000/1.000 | 2.04 | 342 | 89% | 93% |
| 4 | 28 | 1.000/1.000/1.000 | 2.38 | 409 | 98-103% | 94% |

快环在 28 fs 下每个采样 tick 仍是精确的 1ms（982-988 ticks/s），没有
周期拉长——但两个 agent 的单核 CPU 已经顶死。**28 fs 就是 1ms 控制面
的饱和点**，余量为零；50ms 时代"20 fs tick 2.0ms"的包袱主要在慢环里，
快慢分离把它挡住了（dump 每 200 tick 偷走一个 tick，0.5% duty）。

离线 bench（bench_sched.py 在 dpu2 上灌合成流集合，跑生产代码的
demand+entitlements+marker 步）给出墙外的形态：

| fs | 8 | 16 | 28 | 32 | 64 | 128 | 256 | 512 |
|---|---|---|---|---|---|---|---|---|
| 步耗 p50 (µs) | 137 | 252 | 370 | 491 | 978 | 1815 | 3478 | 7002 |

约 13.7µs/fs 线性增长（fastfill 的 O(N log N) 成立，无平方项复发）。
纯调度步在 ~64 fs 越过 1ms；叠加测量/遥测/日志后实用墙 ≈ 32-40 fs，
越墙后周期按比例温和拉长（512 fs ≈ 8ms 周期；50ms 档下 512 fs 仅占
预算 14%）。假设"墙的形态是退化不是崩"成立。

## 三、batch B 第一跑：TCP 执行面两层覆盖缺口（严重，已修）

28 fs 全部放开需求（TCP -b 0、RDMA 不限档）的第一跑，公平性彻底崩溃：
跨对 TCP 每条 12-15G（应得 2.5G），TCP 合计 178G 打满整条 200G 链路，
**每个 dst VM 的 20G MaxRate 被冲到 46-55G**——租户间硬隔离失效（两层
模型的 devlink 硬件兜底未部署，软件层失效后无界）。RDMA 被挤成饿殍
（vf0 全部 ≈0）。响应律全程正常（e 正确压低、R 进 MD 下界），是执行器
不听话。

根因两层，都在 host fq+edt 的覆盖面上：

1. **tc filter 只挂在 dpu1vf0 的 egress**，vf1/2/3 没有 clsact/filter，
   root qdisc 还是默认 mq+fq_codel（EDT 需要 fq 才能兑现 tstamp 延迟）
   ——这三个 VF 出来的 TCP 结构性绕过整个 shaper。
2. **BPF 的 pair_state 只被 v1 apply 工具播种了 4 个直连对**。数据面
   要求 cfg 和 state 同时存在才 pace；shim 运行时只写 cfg，所以就算在
   vf0 上，跨对流量也因缺 state 被静默放行。

两个缺口的共同结构：**执行面对"新流集合"的覆盖不是随控制面自动生长的，
而没有任何告警暴露这一点**。历史上所有 TCP 验收都是 vf0 直连对，07-10
的跨对 TCP 只发 300M（低于 pace 阈值），缺口一直隐形——直到这次把需求
推满才炸出来。

修复（全部入库）：`tools/host/edt_ensure.sh`（幂等：四个 VF 的 root fq
+ clsact + filter 挂载，VF 重建后重跑即可，性质等同 rx_agent 自装的
OpenFlow 规则）；`hpft_pace_shim.py` 启动时对全部 local→remote pair 以
noexist 播种 state（不碰活 pair）。修复即时验证：昨天裸奔 28.5G 的
(1,2) 跨对被压到 16.4G（E 环 cap 生效）。

（诊断途中一度误判"pin 丢失、shim 写孤儿 map"——实际是 pin 路径漏了
maps/ 子目录，非 sudo ls 静默空输出误导。shim 的 fd 一直有效。）

## 四、batch B 第二跑：RP 与响应律的跨层活锁（已修）

修完执行面重跑，TCP 全部落位（±10%），但 RDMA 出现新病：部分流被钉死
在远低于份额处——(1,3) 0.27G、(1,1) 0.76G（应得 3.3G），且不振荡、不
恢复。tx 轨迹显示响应律无辜：R 稳在 4.1-4.7G（hai 探测顶着 ceil×1.05
的锚），预算如实下发，但线上只兑现 6-16%。

机制（device 码只读比对 + 0xdeb 佐证）是一个三方咬合的活锁：

- RP 把**任何**预算变化当 cap 阶跃：比例前馈缩放 level、`hold=3` 冻结
  积分、`last_rrx_used=erx` 吞掉当次速率样本；
- 1ms 下被压穿的流长期处于 hai 探测，其上界锚 ceil 随整个 dst 的速率
  向量每毫秒抖动 → R 每次 flush（~19ms）都变 → 预算单位数每次都变；
- 于是 hold 永不过期（连递减的机会都没有），积分永不迈步，level 只被
  比例缩放——**开局风暴压穿的 level 按比例永远压穿**（0.27G/4.3G）。
  响应律用来救流的探测行为本身把执行器冻住了。

只在规模场景显形，因为要同时满足：转换风暴够大（把 level 压穿）、流
长期回不到份额（持续 hai）、ceil 持续抖动（多流集合）。M2 两三个 fs
时 ceil 安静、预算会静止 3 拍以上，hold 正常过期，所以从没见过。

修复在 tx 侧（不动 device 码）：**mailbox 预算滞回**——每 flowtag 记住
上次写入的预算，漂移 <3% 时重发旧值（rate 字段照常每次新鲜、带抖动），
恢复 RP 设计时的"预算准静态"语义；真实 cap 阶跃（>3%）照常触发前馈+
hold。修复后中途 0xdeb：vf1 三对 lvl/bud 0.90-0.93，wire 全部回到份额。

## 五、batch B 第三跑：28 fs 竞争下的公平性（最终结果）

75-120s 稳态窗，wire 口径（fair_rx.jsonl / analyze_fair.py）。参照值是
手推的两树层级 max-min 均衡（含需求封顶再分配；RDMA 按 0.92 的
budget→wire 兑现率折算）：

- **TCP 16 条全部 ±10%**（-8.2% ~ +8.6%），秒级 sdev ≤0.15。其中 vf2
  的 (2,1)/(2,3) 在第二跑曾到 +46-49%，是 work-conserving 的正确再分配
  （vf2 只有一条 RDMA，让出的类份额被其 TCP 和 dst 侧兄弟吸收）——第三
  跑 RDMA 恢复后回到 ±9%。
- **RDMA**：vf1 三条 3.10-3.13G（参照 3.07，+0.9~+2.1%）；(2,2) 4.42G
  （+13%，grant 4.74 的 93%）；vf0/vf3（各 4 条扇出）1.82-1.94G，比
  参照低 16-21%，且秒级 sdev 0.81-0.97（vf1 只有 ~0.2）。
- 类合计 TCP 38.5G / RDMA 28.6G，聚合 67.1G（本网格的理论上限 ~74G，
  不对称扇出是拓扑性质）；四个 dst 16.3-17.9G，全部守住 20G MaxRate。
- 无饿死（最低 1.82G）、无死锁、无标记风暴复发。

## 六、遗留与挂起

1. **vf0/vf3 型（4 路 RDMA 扇出）残余振荡**：均值 -20%、per-fs sdev
   ~0.85，grant 兑现率 ~87%。方向怀疑在 RP level 动力学与滞回预算的
   相互作用，值得在 D2（极端比例）里顺带观察是否放大。
2. **flowtag 碰撞的根治**（qpn-override 通道）：挂起，等用户决策。
3. 0xdeb 读数偶见 r>bud 且 lvl 在地板（如 (3,2)）——readback 口径问题
   或 RC 重传膨胀，未影响最终账，记录待查。
4. devlink 硬件兜底（两层模型第一层）未部署——本轮 MaxRate 被软件层
   失效冲破 2.5× 的事实是它价值的最好论据。
5. perftest 的 --rate_limit=2.5 在本环境退化为 SW 限速（HW 档拒绝），
   速率有毛刺，阶梯负载判读时注意。

## 产物

- `ladder_run.sh` / `fair_run.sh` / `analyze_ladder.py` / `analyze_fair.py`
  / `bench_sched.py`（runner 与分析）
- `sc_rx.jsonl` `sc_tx.jsonl` `timeline.txt` `cpu_samples.txt`（batch A 原始）
- `fair_rx.jsonl` `fair_tx.jsonl` `fair_timeline.txt` + 客户端
  `fair_tcp_*.json` / `fair_rdma_*.log`（batch B 第三跑原始；第一/二跑
  的 rx jsonl 未存档，结论以本文数字为准）
- `bench_sched.txt`（离线 bench）
- 代码/配置变更：registry `rdma_flowtags`、tx_agent_e（(src>dst) 键 +
  预算滞回）、hpft_pace_shim.py（state 播种）、tools/cross_pair_net.sh、
  tools/host/edt_ensure.sh——repo 与部署已同步，未 commit。
