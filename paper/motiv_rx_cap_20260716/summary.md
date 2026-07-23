# Motivation 1.3：接收端带宽额度上的不公平竞争（2026-07-16）

## 实验是什么

云厂商卖给 VM 一个下行带宽额度。这制造了一类**云环境独有**的瓶颈：
**网络视角下毫无拥塞**（80G 走在 200G 链路上，链路才用 40%），稀缺完全
来自"这台 VM 只买了 20G"这个行政约定。TCP 和 RDMA 在这个额度上相撞。

- **拓扑**：4 个发送 VF（4 个租户）→ **同一个**接收 VF（vf0），全程走
  VxLAN overlay（VNI 100，underlay 172.16.1.1/2）。接收端 vf0 同时持有
  10.1.{0..3}.2，每个源 VF 走自己的子网。
- **发送端**：每 VF `devlink tx_max=20G` → offered 80G < 200G 线速 ✅
  网络不是瓶颈。
- **接收端 20G 额度**：接收端 DPU 的 decap 点上一个 OVS meter
  （`in_port=vxlan100 → meter:1,NORMAL`，20 Gbps，drop band）。这是
  **朴素云策略器**：class-blind、超额直接丢、**不产生任何 ECN**。
- **流量**：4 源 VF × (4 RDMA + 4 TCP) = 32 流，全打 vf0。
- **三臂**：GBN / SR(w=512，默认) / SR(w=1024)，各 3 遍。SR 开关是
  ROCE_ACCL 寄存器（host PF 38:00.1）；窗口是 mlxconfig
  `LOG_TX_PSN_WINDOW`（log2 值，9=512、10=1024，**需 fw reset 激活**）。
- CC 全默认：固件原生 DCQCN + Cubic（无 ECN，beta=717）。

## 结果（每格 3 遍均值，方差极小）

| 臂 | RDMA 合计 | TCP 合计 | 总计 | TCP/RDMA |
|---|---|---|---|---|
| GBN | **2.40G** | 16.74G | 19.14G | **7×** |
| SR (w=512，默认) | **0.99G** | 18.44G | 19.43G | **19×** |
| SR (w=1024) | **1.00G** | 18.43G | 19.43G | **18×** |

逐租户（GBN）：4.57 / 4.87 / 4.89 / 4.76G —— **租户间基本均分**（±4%）。
逐租户内：RDMA 0.60G vs TCP 4.0-4.3G。

## 三个结论

**1. DCQCN 在这里完全失明——0 次触发。** 6 个运行、每个 60 秒，
接收端 CE 标记 **0**、生成 CNP **0**、发送端处理 CNP **0**。策略器只丢包
不打标，DCQCN 的输入信号根本不存在。RDMA 只能靠丢包后的重传兜底。这不是
配置问题，而是**这类瓶颈的固有性质**：合同稀缺不产生拥塞信号。（比较对象
jakiro 的 DHTB 之所以要在 RED 时专门给 RoCE 打 CE，正是为了补这个缺陷。）

**2. 租户间公平、租户内崩坏。** 4 个租户各拿 ~4.8G（近似均分 20G 额度），
但每个租户内部 TCP 拿走 87%。原因是两类对"纯丢包"的反应能力差了一个量级：
Cubic 有快速重传 + 0.7 温和退避，丢包是它的设计工况；RDMA 没有 ECN 信号，
只能靠 NAK/超时重传，60 秒内 **35 万次乱序事件**，goodput 塌到 0.6G/租户。

**3. SR 不但没救 RDMA，反而更糟（2.40G → 0.99G，劣势从 7× 扩到 19×）；
把 SR 的窗口从 512 加倍到 1024 也毫无变化（1.00G）。**

SR 更差可解释：GBN 无窗口约束地盲发，在**纯丢包的自由竞争**里反而抢到更多
份额（代价是 13-15% 丢弃率与更多重传垃圾——GBN 线上送达 3.61G 但 goodput
只有 2.40G，1.2G 是重复重传）；SR 精确补洞、ACK 时钟驱动，更克制 = 拿得少。
与 1.2 的 m5 档"SR 太礼貌"一致。

**窗口加倍无效的原因（重要）**：SR 的 offered 只有 **1.75-1.83 Gb/s**，而
512 包窗口（≈0.5MB，RTT ~50µs）本身允许约 **84 Gb/s**——窗口比实际发送量
高出 45 倍，**根本不是约束**。限制 SR 的是丢包后的恢复循环，不是窗口大小。
所以 w=512→1024 不动是预期内的、且与数据自洽（三遍重复，方差 <0.03G）。
旋钮确已生效：`LOG_TX_PSN_WINDOW` fw reset 后 current=10。

**综合含义很强：换重传算法、加大窗口，都救不了接收端的这个问题，
因为病根是缺信号，不是恢复效率、也不是窗口容量。**

## 机制证据（每个运行都采）

| 指标 | GBN | SR(512) | SR(1024) |
|---|---|---|---|
| 接收端 CE 标记 | 0 | 0 | 0 |
| 接收端生成 CNP | 0 | 0 | 0 |
| 发送端处理 CNP | 0 | 0 | 0 |
| RDMA 发出 / 送达 | 4.1-4.4 / 3.6-3.8G | 1.7-1.8 / 1.6-1.7G | 1.8-1.8 / 1.7G |
| RDMA 丢弃率 | 12.6-14.6% | 5.7-7.2% | 5.9-7.1% |
| RDMA 乱序/NAK 事件 | ~35.2 万 | ~37.2 万 | ~37.1 万 |
| TCP 重传段数 | ~650 万 | ~710 万 | ~700 万 |
| p1 线上到达 | ~23.9G（含 VxLAN 头） | ~23.3G | ~23.3G |
| meter 放行 | ~21.2G | ~20.9G | ~20.9G |

注意 offered 只有 ~24G 而非 80G：这是**稳态**——两类 CC 在丢包压力下已经
把发送速率压下来了；80G 是需求上限，不是稳态速率。

## 关键平台事实（复现必读）

1. **裸 RoCE 无法在接收端限速**。实测：devlink `tx_max` 只限上行
   （上行 18.49G ✓ / 下行 92.52G ✗，且 devlink 无任何 rx/ingress 参数、
   物理上行口不是 rate 对象）；TC police 对 TCP 有效（19.13G）但
   **RoCE 完全绕过（92.53G，规则在硬件里、命中 0）**——网卡的 RDMA
   steering 把 RoCE 直送 QP，不经过 eswitch 的 FDB/TC。
2. **必须靠 VxLAN 封装**：线上是 UDP/4789，RDMA steering 不认，流量才
   必须走 eswitch，两类才能被同一个 meter 管住。这正是 jakiro
   （`/home/ubuntu/bzx/jakiro_dhtb`，本项目比较对象）把
   "It must consider VxLAN encapsulation" 列为硬需求的原因，也是真实云的
   样子（租户流量跑 overlay，基础设施在 decap 点执行额度）。
3. **VxLAN 硬件卸载的前提**：underlay IP 必须**直配在上行口 p1 上、p1 不
   入桥**。按"underlay 桥 + internal 口配 IP"搭会报
   `failed to offload flow: Invalid argument: p1`，退回 ARM 软件路径，
   RoCE 只有 **1.12G**；改成 NVIDIA 标准姿势后 **88.56G**。
4. **TC police 装不上**（mlx5 要求规则含 forward/drop 动作，
   `conform-exceed continue` 不满足）→ 改用 **OVS meter**。
   我们没让 DOCA Flow 接管 decap，正是 jakiro HANDOFF 建议 #2
   （它自己实测 DOCA Flow 拥有 decap 会把吞吐从 360G 打到 160-190G）。
5. **策略器超发**：配 20 Gbps，实际放行 ~21 Gb/s（≈5%）。重过载时更明显
   （早期 90G 灌入时测到 21.21G goodput > 20G）。原因是令牌桶 burst +
   硬件粒度；policer 本就不是精确整形器。三档 burst（默认/2000/200）
   实测差别不大，保留默认。**引用"20G 额度"时应标注实测放行 ~21G。**
6. **SR 寄存器必须设在 host PF（38:00.1），不是 DPU（03:00.1）**。
   我第一轮误设在 DPU 上，导致"SR 臂"其实还是 GBN（两臂数据一模一样是
   识别信号），已作废重跑。用 `tools/cc_mode.sh sr|gbn`（在 mimd 分支）。

## 数据与复现

- `runs/<arm>_run<N>/`：32 流原始输出 + 双端计数器快照（host/peer/dpu2
  的 pre/post）+ 跑动中 ping + vpm 时间序列。
- `parse.py` → `perflow.csv`（384 行逐流）、`runs.csv`（6 运行 × 15 指标）。
- `plot.py` → `fig_rx_cap.png/pdf`（三臂类合计堆叠柱）。
- `setup_topology.sh`：从干净/刚 fw reset 的状态一键重建整套测试床
  （VF+GUID / VxLAN overlay / 20G meter / 发送端限速 / 连通性自检）。
- `run_point.sh`：跑一个点（`OUTROOT=runs bash run_point.sh gbn_run1`）。

## 实验后 lab 状态

VxLAN overlay 与 20G meter 是本实验专用拓扑，实验后已拆除并复原为 1.2 的
直连 VLAN 结构（见 run_log）。

## 复原记录（2026-07-16）

- VxLAN overlay(ovsbr-p1/vxlan100) 已删；p1 IP 清除、MTU 回 1500、重新入
  underlay-p1 桥；4 个 VF representor 归位。
- 发送端 devlink tx_max 全清零（0 项残留）。
- 接收端 VF IP 复原：vf0 只留 10.1.0.2，vf1-3 各自 10.1.{1,2,3}.2。
- OVS meter 已随 ovsbr-p1 一并删除（残留 0）。
- 基线态核对：UPCC=0 / SR=0（纯默认固件 DCQCN+GBN）、E 栈全停、
  4 对直连路径连通。
- **注意：dpu2 的 p1 现为 200G**（1.3 规格要求）。1.2 的实验基线是 100G，
  复跑 1.2 前需 `ssh hpft-dpu2 'sudo ethtool -s p1 speed 100000 duplex full'`。

## 扩展：Jakiro DHTB 作为接收端额度执行器（2026-07-16）

把 1.3 的朴素策略器（只丢包）换成比较对象 **Jakiro DHTB**（论文机制：
两层令牌桶 + 借用 + 对 greedy RoCE 打 CE、对 greedy TCP 丢弃）。代码在
`/home/ubuntu/bzx/jakiro_dhtb`，为本环境从 DOCA 2.x 移植到 DOCA 3.4
（9 类 API 迁移，见下）。拓扑同 1.3（VxLAN + 4 源 VF → vf0），但 Jakiro
是"单 Jakiro=单 overlay IP"，故 32 流全打 10.1.0.2；4 源 VF 改到同子网
10.1.0.{11..14}。CAPACITY=20G，权重 1:1。

### 机制确认：Jakiro 造出了信号（对比 1.3 的 CNP=0）

单流探针与满载都证明判决树工作：`ce_marked_roce_forward` 达 1.9 亿包
（57% 的 RoCE 被打 CE），`tcp_greedy_root_red_drop` 数百万，**发送端
rp_cnp_handled 从 1.3 的 0 变成 36 万–243 万**。即 Jakiro 让接收端
CE→CNP→发送端 DCQCN 的环路复活——这正是它相对朴素策略器的本质区别。

### 但核心发现：默认 GBN-DCQCN 对这个信号的响应是**双稳态、不稳定**的

Jakiro 对 greedy RoCE 只**标记不丢**（对 TCP 才丢），所以 RDMA 总量取决
于端侧 DCQCN 收到 CNP 后降速的力度。TCP 因为被硬丢，始终稳定在 ~10G。
RDMA 类稳态（多次重复，每次全新 QP）：

| 臂 | RDMA 稳态（G，多次重复） | TCP | 判读 |
|---|---|---|---|
| **GBN** | **28.5 / 10.2 / 45.2 / 45.8**（双稳态） | ~10G | 同配置随机落入公平(10G)或失控(28-46G) |
| **SR** | **10.0 / 10.1 / 10.0**（3/3 稳定） | ~11G | 每次都收敛到加权份额 |

逐流看：16 条 RDMA 流总是**一致行动**（同步进入受控或失控盆地），不是
个别流失控——是系统级双稳态。失控态下 RDMA 送进 VM 27.8-49G（标称 20G
额度的 1.4-2.5 倍），CE 标记高达 1.9 亿/60s 但 GBN 的 DCQCN 压不住。

**给论文的 Jakiro 缺陷论点**：Jakiro 造了信号，但它对 RDMA 的执行是
"软"的（标记而非丢弃），把公平性完全外包给端侧 CC。默认固件 DCQCN(GBN)
对这个信号的响应不稳定——同一配置，RDMA 可能公平(10G)也可能失控(45G)。
只有换成 SR（响应更稳）才每次收敛。即：**Jakiro 的加权公平不是自足的，
依赖一个恰好配合良好的发送端 CC**。

图 `fig_jakiro_ts.png/pdf`：接收端硬件 vport 速率时间序列，左 GBN（RDMA
冲到 49G 全程压不下）、右 SR（两类精确贴 10G）。红虚线=10G 加权份额。

### DOCA 2.x→3.4 移植要点（复现用）

9 类 API：`actions.action_idx`→`pipe_basic_add_entry` 参数；
`pipe_add_entry`→`pipe_basic_add_entry`（去 actions_mask）；
`control_add_entry` 的 priority 参数移到 monitor 之后；
`shared_resource_cfg.domain` 移除；`parser_meta.port_meta`→`port_id`；
`ctx->doca_dev`→`ctx->devs_ctx.devs_manager[0].doca_dev`；
`init_doca_flow_ports`→`init_doca_flow_switch_ports`（加 actions_mem_size/
resources）；`init_flow_switch_dpdk`→`flow_init_dpdk`+`init_doca_flow_devs`；
`isolated_mode` 移除；`destroy_doca_flow_switch_common`→`destroy_doca_flow_devs`。
另需：`-p`→`-a`（新 argp）、大页 `vm.nr_hugepages=2048`、controller-1 的
host representor argp 查不到（改为按 host PCI 直接 doca_dev_rep_open，见
main.c 的 jakiro_open_rep_by_pci，env JAKIRO_REP_PCI）、jumbo 需
mbuf_size=9800。TCP 分类器放宽为按 IP 协议号（next_proto=TCP）匹配以覆盖
16 个 iperf3 端口。原 DOCA2 版备份为 src/*.bak_doca2、apply_*.bak_doca2。

### 数据
`runs_jakiro/`：gbn_run{1,2,3}/gbn_run1b/gbn_cold、sr_run{1,2,3}、
seq_{A,B,C}（GBN 双稳态对照）、srseq_{A,B,C}（SR 稳定对照）。
`run_point_jakiro.sh`（含 jakiro 分支计数采集）、`plot_jakiro_ts.py`。

## 走 2：Jakiro 的公平性由 DCQCN 恢复激进度决定（剂量-响应）

固定 Jakiro（20G，1:1）、GBN，扫发送端 DCQCN 恢复参数（host PF sysfs，
1.2 同款三档），每档全新 QP + 全新 Jakiro meter 状态跑 4 次：

| DCQCN 档 | rpg_time_reset/ai/hai | 4 次 RDMA 稳态 (G) | 收敛到公平 |
|---|---|---|---|
| gentle | 1200/1/10 | 9.6 / 9.6 / 9.6 / 9.6 | **4/4** |
| default | 300/5/50 | 28 / 10 / 28 / 46 | 1/4（双稳态） |
| aggressive | 75/50/500 | 63 / 64 / 61 / 63 | **0/4** |

完美单调：**恢复越温和，Jakiro 加权公平越稳；越激进，RDMA 越失控**
（aggressive 档 RDMA 恒定冲到 ~63G = 20G 额度的 3 倍多）。因为 Jakiro
对 greedy RoCE 只打 CE 不丢，RDMA 的稳态速率完全由"DCQCN 收到 CNP 后
退得多狠、爬得多快"决定：gentle（降后慢爬）被 CE 稳稳压住；aggressive
（降后猛爬）每次都把队列/额度顶穿。

图 `fig_dcqcn_sweep.png/pdf`：x=DCQCN 恢复档，y=RDMA 稳态（每档 4 点，
绿=公平/红=失控），标 10G 份额线与 20G 额度线。

**给论文的最终 Jakiro 论点**：Jakiro 造出了信号，但把公平性的达成完全
外包给发送端 CC 的响应特性。它的加权公平既不是即时的（走 1：收敛需要
DCQCN 环路稳定，期间 RDMA 可 2-3× 超额），也不是自足的（走 2：默认
DCQCN 下双稳态，只有恰好足够温和的恢复参数才 4/4 收敛）。这正是需要
一个不依赖端侧 CC 善意配合的方案的动机。数据在 `dcqcn_sweep/`。
