# Motivation 1.2：异构 CC 在 incast 下的收敛由算法设计决定（2026-07-14/15）

## 实验是什么

把 32 条流（4 对 VF，每对 4 条 RDMA + 4 条 TCP，两类权重天然 1:1）同时
打向 100G 瓶颈：接收端 dpu2 的 p1 降到 100G，发送端保持 200G，拥塞固定
发生在交换机 swp37s0 的出口队列。RDMA 是 lossy RoCEv2（PFC 与全局 pause
双端全关），拥塞控制为**网卡固件原生 DCQCN**（非 PCC）；TCP 是 Cubic、
不开 ECN、参数全默认；两类流量共享同一个 TC0 队列（用交换机每 TC 计数器
验证过：只有 TC0 在动）。每个数据点跑 60 秒，读每条流自报 goodput，同时
取交换机 ECN 标记/丢弃差分、双端 PF 的 CNP 计数差分、发送端乱序计数差分、
以及跑动中的 fabric RTT（队列深度证据）。

两个子实验，各自扫一个维度，都在 GBN 和 SR 两种丢包恢复下各跑一遍
（共 14 个点）：

- **A：交换机 ECN 打标参数**。激进 36KB/108KB/80%、默认 108KB/396KB/20%
  （即用户指定的 Kmin=100K/Kmax=400K/Pmax=20% 按交换机 cell 取整）、
  温和 396KB/1.54MB/5%——阈值扫了 16 倍范围。
- **B：速率恢复的激进程度**。RDMA 侧改 DCQCN 的恢复参数
  （rpg_time_reset/rpg_ai_rate/rpg_hai_rate：慢 = 1200µs/1/10，
  默认 = 300µs/5/50，快 = 75µs/50/500）；TCP 侧改 cubic 的退避系数
  beta（慢 = 410/1024，默认 = 717/1024，快 = 922/1024）。

## 总结果表

| 配置 | RDMA 类合计 | TCP 类合计 | 跑动中 RTT | ECN 标记数 | CNP 数 | TCP 重传 |
|---|---|---|---|---|---|---|
| gbn ecn 激进 | 0.33G | 93.80G | 2.7ms | 265 万 | 45 万 | 7.5 万 |
| gbn ecn 默认 | 0.34G | 93.73G | 3.2ms | 296 万 | 47 万 | 7.1 万 |
| gbn ecn 温和 | 0.29G | 93.77G | 2.5ms | 255 万 | 40 万 | 10.3 万 |
| gbn rdma 慢恢复 | 0.06G | 94.15G | 3.0ms | 88 万 | 12 万 | 6.3 万 |
| gbn rdma 快恢复 | 2.24G | 91.56G | 2.9ms | 1954 万 | 311 万 | 11.0 万 |
| gbn tcp 慢恢复 | 0.34G | 93.74G | 1.6ms | 293 万 | 42 万 | 6.8 万 |
| gbn tcp 快恢复 | 0.32G | 93.43G | 3.4ms | 432 万 | 50 万 | 14.7 万 |
| sr（同七点，真 SR） | 0.06–2.40G | 91.7–94.1G | 1.6–3.1ms | 同量级 | 同量级 | 同量级 |

（SR 七点为 2026-07-15 用 ROCE_ACCL 寄存器真开 SR 后重测：与 GBN 逐点
几乎相同——ECN 三档 0.26/0.26/0.34，恢复慢/快 0.06/2.40，TCP 慢/快
0.32/0.34；且 SR 轮发送端 packet_seq_err 全程为 0（GBN 轮 4-261），
佐证重传模式确实切换了。完整数字在 `motiv12_perflow.csv` 与
`motiv12_runs.csv`。）

## 三个结论（附价值判断）

**1. 默认配置下 DCQCN 被 Cubic 饿死到 0.3%——这是本节最有力的单点事实，
值得做主图。** 16 条 TCP 拿走 93.7G，16 条 DCQCN 流合计只剩 0.34G（每流
21Mb/s，比 1:1 的公平份额低 170 倍）。机制链条每一环都有计数器背书：
TCP 不吃 ECN 也几乎不丢包（交换机深 buffer 把队列养到 ~40MB，跑动中
RTT 从 0.04ms 涨到 3.2ms），队列因此永久高于 Kmax=396KB；于是 RoCE 数据
帧几乎逐帧被标 CE（296 万标记 ≈ RDMA 帧数），接收端 PF 60 秒发出 47 万个
CNP，发送端 PF 逐个处理（收发两侧 CNP 计数完全对账）；DCQCN 的 alpha 被
钉在 1，永远处于降速状态，恢复步长杯水车薪。同一瓶颈、同一队列，两个
算法读到的世界完全不同：Cubic 看到的是"无损网络"，DCQCN 看到的是
"永久拥塞"。

**2. ECN 参数扫 16 倍救不了它——"调参数"这条评审退路被实测堵死。**
激进/默认/温和三档下 RDMA 都在 0.27-0.34G，TCP 都在 93.7-93.9G，
连 RTT 都只在 2.2-3.2ms 之间晃。原因很朴素：不公平的根源不是阈值高低，
而是**信号的命运不对称**——ECN 只对带 ECT 的 RoCE 生效，TCP 既不进
标记也几乎不进丢弃（tail-drop 被深 buffer 推迟到端侧重传计数器里才
看得见，交换机队列丢弃计数整轮为 0）。只要 TCP 能把队列顶穿任何
Kmax，三套参数就都是同一套。

**3. 恢复激进度只能改变"渣有多大"，改不了"谁吃肉"。** RDMA 恢复参数
从慢到快扫 30 多倍，RDMA 份额相应从 0.06G 变到 2.24G（CNP 从 12 万涨到
311 万——更激进的恢复只是更频繁地撞墙），但 TCP 始终 >91G；TCP 侧 beta
从 0.4 扫到 0.9，分配纹丝不动（TCP 内部：beta 大→队列更深 RTT 3.4ms、
重传翻倍到 14.7/21 万；beta 小→队列减半到 1.3-1.6ms），因为 TCP 的损失
事件太稀疏，beta 根本不常进场。**GBN↔SR 逐点无差（真 SR 实测）**：
RDMA 几乎不丢包（GBN 轮乱序 ≤261、SR 轮为 0），这个饿死机制是标记主导
而不是丢包主导，换重传算法无关痛痒——"上 SR 就够了"这条评审退路被
实测堵死。

## 追加验证（2026-07-15）：SR 的开启方法与重测

用户对"GBN↔SR 逐点无差"提出怀疑。排查确认：初版实验用的
`RDMA_SELECTIVE_REPEAT_EN` mlxconfig 旗标**不会改变重传行为**（判决
探针：单流 × 深队列 RED 4-8MB/2%，把在途撑到 ~8MB——旗标两档下每次
NAK 都浪费 ≈5.8MB 整窗，行为恒为 GBN），初版的 sr_* 七点实为 GBN 的
重复测量，已归档到 `_obsolete_flagera_sr/`。

顺带拿到一组独立有用的数据（16 纯 RDMA 流 incast，GBN）：关 ECN 纯
tail-drop 下 33.15G、9.3 万 NAK、线上 66% 是重传垃圾——"lossy RDMA
没有拥塞信号就不可用"的量化证据。

### 终版（2026-07-15 晚，用户同学供方法）：ROCE_ACCL 寄存器直接强制 SR

`sudo mlxreg -y -d 38:00.1 --reg_name ROCE_ACCL --set
"selective_repeat_forced_en=0x1"`（双端 host 各一次，秒级生效、无需
fw reset、**DCQCN 原样保留**）。三重验证：SR 指纹（lowred
1.3 万次丢包线上零浪费）、DCQCN 兼容（glacial RP 参数照常拍到 0.003G）、
与 mlxconfig 旗标完全无关（flag=0 下照样 SR）。注意寄存器易失，fw
reset 后要重设。封装成 `tools/cc_mode.sh sr|gbn`。

**干净的 16 流 GBN vs SR（同 DCQCN、同普通建连，rdmaonly3_*）**：

| 队列配置 | DCQCN+GBN | DCQCN+SR（寄存器） |
|---|---|---|
| 默认 ECN | 92.48G | 91.20G（-1.3%） |
| 关 ECN 纯 tail-drop | **32.28G**（7.1 万 NAK、RTT 4.8ms、线上满线速 2/3 重传垃圾） | **91.20G**（零 NAK、RTT 0.77ms） |

给论文的表述从此干净了：GBN 连"自己人的 incast"都撑不住（无信号
tail-drop 下崩 2.8 倍），SR 完全治愈——且这次两臂只差重传算法本身。
附带观察：forced SR 的发送管线在途更克制（noecn 站立队列稳在 ~9.6MB
不打爆缓冲；deep-RED 4-8MB 探针下队列甚至不过阈值、零丢包），解读
微小差异时留意。

### 1.2 主实验 SR 半边的重测（2026-07-15，本表已并入总结果表）

寄存器方法确认后，把 1.2 的 SR 七个点全部真跑了一遍（预检 + GBN 可
复现性抽查通过：`gbn_ecnneutral_repro` 0.27G/93.92G，在原扫描噪声带
内，故 GBN 七点沿用原数据）。结果与 GBN 逐点几乎无差（ECN 三档 RDMA
0.26-0.34G、恢复慢/快 0.06/2.40G、TCP 慢/快 0.32/0.34G），且 SR 轮
发送端 packet_seq_err 全程为 0（GBN 轮 4-261）——重传模式确实在切换，
只是这个饿死机制根本不经过丢包。结论 3 的"换重传算法无关痛痒"从此有
真数据背书。

## 扩展实验（2026-07-15 晚）：ECN 温和阶梯——"还 RDMA 公平"的剂量-响应

用户方向：把现默认档定位为最激进锚点，向上铺更温和（对 RDMA 更公平）
的档位，观察 RDMA（尤其 SR）能否拿回像样的份额。关键设计洞察：混跑时
TCP 把队列养到 ~40MB（rmem 限制的均衡点），**Kmax 不越过这个运行点，
一切温和都是假温和**。混跑 16+16，其余配置同 1.2 主实验，图
`motiv12_fig_ecnladder.png/pdf`：

| 档位 | Kmin/Kmax/Pmax | GBN: RDMA / TCP | SR: RDMA / TCP |
|---|---|---|---|
| E0（主实验默认档） | 108K/396K/20% | 0.34 / 93.7G | 0.26 / 93.9G |
| E1 | 1M/4M/20% | 0.28 / 93.3G | 0.27 / 93.9G |
| E2 | 4M/16M/10% | 0.24 / 76.9G(异常点) | 0.34 / 93.8G |
| E3 | 16M/48M/5% | 6.72 / 86.7G | 2.56 / 91.5G |
| E4 | 无 ECN，纯 61MB taildrop | **32.97 / 2.72G（同归于尽）** | **16.16 / 77.41G（干净共存）** |

四个发现：

1. **阈值不过 TCP 运行点就全无效**：E0→E2 扫了 40 倍阈值，RDMA 钉死在
   0.24-0.34G——印证"信号命运不对称"的结构性结论。E2-GBN 是个不稳定
   交界点（CNP 1.8M、乱序 2.2 万、TCP 掉到 76.9G），标记开始有牙齿但
   还不够，GBN 风暴与重标记互相激励，值得复测确认双稳态。
2. **E3（Kmax=48M > 40M 运行点）出现拐点**：RDMA 回血到 2.6-6.7G。
   注意 GBN（6.72）反而高于 SR（2.56）——SR 的 512 包窗口纪律让它
   在"按队列占有率分配"的竞争里更礼貌、份额更小。
3. **E4 是本轮的重磅**：GBN 下同归于尽（RDMA goodput 33G 但线上 98.8G
   有 64% 是重传垃圾，TCP 被屠到 2.7G，系统总 goodput 35.7G，乱序
   7.7 万）；SR 下**干净共存**（16.2 + 77.4 = 93.6G，RDMA 零乱序，
   TCP 重传 2.1 万为全部配置最低）。GBN vs SR 的对比从"份额差异"升级
   为"网络存亡差异"。
4. **SR 的份额是结构性的、可预测的**：16 QP × 0.58MB（512 包窗口）≈
   9.3MB 队列占有 vs TCP ~40MB → 预测份额 19%，实测 16.2G/17% 吻合。
   即：就算调到最优点，RDMA 的份额也是"窗口大小 ÷ 缓冲深度"的算术
   偶然，不是任何政策的产物；且所有"公平"档位的代价是 2.1-4.7ms 的
   站立队列 RTT——RDMA 的低延迟本性已死。公平与低延迟在共享队列里
   不可兼得，依旧支撑主命题。

## 扩展实验 2（2026-07-15 深夜）：缓冲深度阶梯——否定结果，但收敛出结构定律

方法：经 per-port mapping 把我们两口的流量隔离到全网无人使用的 TC5
（EQM SP5→TC5 + TC5 egress alpha，全局改动已在实验尾复原），队列上限
扫 alpha {1/32,1/16,1/8,1/4}（标称 ~2/4/8/14MB），ECN 阈值等比
（Kmin/Kmax = 上限的 10%/40%，Pmax 20%），混跑 16+16 × {GBN,SR}。

结果（gbn/sr_b{2,4,8,16}m 八个目录）：**全部档位 RDMA 仍被饿死在
0.28-0.40G，GBN≈SR（RDMA 乱序 ≤496），TCP 恒 93.6-93.9G**。跑动中
RTT 随档位单调变化（1.15→2.6ms），说明 cap 在起作用，但换算队列
（14-32MB）比标称上限大数倍——alpha 的实际语义待查（下轮可在跑动中
直读 `nv show interface swp37s0 qos buffer egress-traffic-class`）。

价值判断：这轮否定结果把规律收敛成一句话——**只要 Kmax 显著低于 TCP
的队列运行点（= 队列上限本身），缓冲多深都一样：TCP 总能把队列顶到
自己的丢包点，而那一定在 RoCE 的全标记区之上**。E0-E3（阈值维度）+
b2-b16（缓冲维度）共 12 个配置，唯一让 RDMA 破 1G 的是 E3——它是
唯一 Kmax 追平 TCP 运行点的配置。推论：修正方向不是继续调缓冲，而是
**把标记带贴着队列上限配**（Kmin/Kmax ≈ 上限的 50%/100%）：小缓冲 +
贴顶标记应同时给出部分标记（RDMA 有份额）与真实溢出丢包（GBN/SR 差异
在 DCQCN 正常工作下显现）。

## 扩展实验 3（2026-07-16）：固定队列上限，标记带贴顶 + 硬件对账

固定 alpha_1_4（实测单队列上限 42.8MB，交换机 buffer 计数器直读），扫
标记带位置；并新增接收端硬件测量通道（dpu2 vport-meter 的 per-VF
ib/eth 分桶，1Hz 时间序列存 vpm_series.csv，run_point.sh 自动采集）。

| 档位 | GBN: RDMA / TCP / 总 | SR: RDMA / TCP / 总 | GBN 乱序 | CNP(gbn/sr) |
|---|---|---|---|---|
| M4=75%/126% ×3 次 | 2.0-9.5 / 82-91 / 85-92G | 5.0-9.7 / 85-89 / 92-94G | 338-690 | 25-35万 / 35-36万 |
| M5=100%/150% | 22.4 / 64.5 / **86.9G** | 20.0 / 74.0 / **94.0G** | **6694** vs 1195 | 33万 / 3.4万 |

五个结论：

1. **M4 首对的"SR 2.5 倍优势"是轮间方差，撤回**：三次重复 GBN
   {2.0,4.6,9.5}、SR {5.0,5.3,9.7}，分布重叠。硬件时间序列显示 M4 区
   的 RDMA 份额在几十秒尺度上游走（部分标记下 DCQCN 慢动力学），60s
   窗口测不出稳定份额，要论 M4 得跑 ≥180s。
2. **硬件对账立刻抓到了真正的 SR 优势——线上税**：同样 goodput 下
   （m4r3：9.46 vs 9.72G），GBN 的 RDMA 线上量 13.2G、SR 10.8G——
   **GBN 每 1 bit goodput 多烧 30-40% 线路（重传垃圾），SR 只有
   9-11%（≈纯协议头）**。这笔税直接挤占 TCP：每一对比中 SR 的系统
   总 goodput 都更高（+1.6 到 +7.1G）。
3. **M5（Kmin=上限）给出大份额 + 真丢包下的系统级差异**：RDMA 份额
   20-22G（约公平份额一半），GBN 6694 次乱序 vs SR 1195；系统总
   goodput SR 94.0 vs GBN 86.9（+7.1G），TCP 在 GBN 下少拿 9.5G。
   注意边界：GBN 下 DCQCN 仍充分工作（CNP 33 万），SR 下标记稀疏
   （3.4 万，非零但弱）——引用时需说明。
4. **硬件时间序列的定性差异可直接进图**：sr_m5 全程钉在 21.0-21.7G
   纹丝不动；gbn_m5 首 10s 冲到 47.7G 后在 26-28G 抖动。SR 的稳定性
   vs GBN 的漂移一目了然。
5. 代价不变：M4/M5 的跑动中 RTT 2.8-3.6ms（队列贴上限）。

## 与上一轮 motivation（motiv_coexist_20260711）的关系

上一轮的 shared 配置（Kmin=156KB、Pmax=100%、瓶颈 25G）里 RDMA 拿 TCP
的 3 倍；这一轮的默认配置里 RDMA 被压到 0.3%。**同一对协议、同一台交换
机，配置细节（阈值、概率、buffer 语义、瓶颈速率）一变，受害者直接换
人**——这比任何单向结论都更有力地支撑论文命题："谁拿多少"是两个 CC
算法与交换机配置碰撞出的副产品，不受任何人的政策意图控制。写论文时
建议两轮数据并排引用。

## 图

- `motiv12_fig_ecn.png/pdf`：子实验 A 主图。上排 2 面板（GBN|SR）逐流
  堆叠柱（16 橙 TCP + 16 蓝 RDMA），下排 RDMA 类合计的对数坐标条形图
  ——线性坐标下 RDMA 根本看不见，这本身就是图的论点。
- `motiv12_fig_cc.png/pdf`：子实验 B 主图，同布局，五档（RDMA慢/默认/
  RDMA快/TCP慢/TCP快）。
- 配色沿用上一轮的 Okabe-Ito 蓝橙对（色盲友好），逐流细黑边框，
  PDF 为矢量版可直接进论文。

## 数据与复现

- `motiv12_perflow.csv`：448 行逐流 goodput（14 配置 × 32 流）。
- `motiv12_runs.csv`：每配置的交换机 TC0/TC3 帧数/标记/丢弃差分与端侧
  乱序计数差分。
- 每个 `gbn_*` / `sr_*` 目录：32 条流原始输出（perftest log / iperf3
  JSON）、交换机 qos 计数器快照（q_pre/post）、端口计数器快照
  （ifc_pre/post，前 3 个 GBN 点没有）、双端 PF/VF RDMA 计数器快照
  （host/peer_pre/post，含 rp_cnp_handled / np_cnp_sent /
  np_ecn_marked_roce_packets）、跑动中 ping（ping.txt）。
- `run_point.sh`（跑一个点）、`motiv12_env.sh`（切 ECN/beta/DCQCN 档位）、
  `post_reboot_recover.sh`（fw reset 后恢复）、`parse_runs.py`、
  `plot_motiv12.py`、`switch_config_backup.txt`（实验前全量备份）、
  `run_log.md`（时间线与环境定案）。

## 关键环境事实（复跑必读）

1. **`USER_PROGRAMMABLE_CC=1` 时停掉 PCC app ≠ DCQCN。** 固件回落到内部
   DPA 算法：贴线速、零丢包、~100% 标记却无 CNP 响应、无视一切 ECN/RP
   参数。本实验把双端 DPU 设 `USER_PROGRAMMABLE_CC=0` 后才得到真 DCQCN
   （标记率立刻从 100% 掉到 0.06%、CNP 环路对账）。**上一轮
   motiv_coexist 是在 =1 下跑的，其 RDMA 侧行为语义与真 DCQCN 的差异
   需要在论文引用前复核。**
2. **mlxconfig 改动必须 `mlxfwreset` 才生效，Arm reboot 无效。** 流程：
   host 侧 `echo 0 > sriov_numvfs; mlxfwreset -d 38:00.0 --yes --sync 1
   reset`（Arm 随之重启 ~4 分钟），然后 VF 重建 + MTU 1500 + dpu2 p1
   100G + 双端 pause/PFC 重关。
3. **host VF 的 DCQCN RP 参数只认 host PF（sgpu01 的 `dpu1` netdev，
   38:00.1）的 ecn sysfs**；DPU Arm 侧 p0/p1 的 sysfs、debugfs cc_params
   对 VF 流量全部无效（逐一探测排除）。GBN↔SR 用 ROCE_ACCL 寄存器
   （`cc_mode.sh sr|gbn`，秒切、无需 fw reset、易失）。
4. TCP beta 在 `/sys/module/tcp_cubic/parameters/beta` 运行时可写；
   `bic_scale`（增长系数 C）只读且 cubic 内建于内核，不可调——所以
   TCP 侧"增长幅度"这条腿实际可调的只有退避/恢复深度。
5. 交换机三档 ECN profile：`ecn_incast_bzx`（默认档，实验前后都绑着）、
   `motiv12_aggr`、`motiv12_gentle`（留在交换机上未删未绑，复跑直接绑）。

## 实验后 lab 状态（已全量复原并验证）

双端固件已设回 `USER_PROGRAMMABLE_CC=1` + GBN 并 fw reset 激活；VF/MTU/
100G 瓶颈/EDT fq+BPF/遥测 underlay 走 reboot_recover.sh 恢复；pace-shim、
RP、rx/tx agent 全部重启（注意：pace-shim 与两个 agent 都是 systemd-run
临时单元，stop 即销毁，重启命令见 run_log.md）；soak 与看门狗 cron 已
解除注释。E2E 验证：单流被 pace 到 49.95G、RP 有活跃预算、tx 代理正确
跟踪 flowset。验证时 tx 短暂 fail_open，是慢性空载 RTT 尖峰所致
（ops_notes 病历，自愈型），看门狗会持续记录。
