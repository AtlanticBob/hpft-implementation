# Swift 拥塞控制项接入 HPFT PCC 执行面（2026-08-27）

**一句话结论**：`0xccd 3` = Swift 已在 DPA 执行面上跑通并按本 fabric 校准；单流跑满 VF 上限、6 对打一个 200 G 口份额均分（Jain 0.9996）且队列稳定在目标附近；与 TCP 同队列时 RDMA 被压到口的 0.26%（比 DCQCN/ZTR 更狠），可以作为 motivation 1-2 的第三种 RDMA CC。仍有两处要知道：（a）Swift 只认时延，6 对场景口利用率 84%，比 DCQCN 行的 98% 低；（b）4 对 incast 到**一个 50 G VF** 的瓶颈是接收端 OVS 丢包型限速器，没有排队时延，Swift 只能靠 NACK 减半，goodput 只有 15 G/50 G——这是场景本身没有时延信号，不是实现问题。

## 一、代码交付（`tools/dpu/pcc/rp_rtt_template_dev_main.c`，四台 DPU 已同步编译）

| 项 | 内容 |
|---|---|
| Swift 项 | `HPFT_CC_SWIFT=3`，`hpft_swift_step()`：每个 RTT 事件跑一步；窗口 `sw_cwnd`（字节）→ 速率 `cwnd / (rtt_s · N)`（N = 该流对的活跃 QP 数，所以一个流对 = 一条 Swift 流，不管它有几个 QP）；目标 `target = base + clamp(α/√cwnd_pkts − β, 0, fs_range)`；`rtt < target` 加 `ai`/RTT；否则且距上次减速 ≥ 1 RTT 时乘 `1 − min(b·(rtt−target)/rtt, max_mdf)`；NACK 视为丢包，同一门控下乘 `1 − max_mdf`；CNP 不理（Swift 只看时延） |
| 探针节奏 | Swift 项在每个 RTT 事件返回时立即再发一个探针（每流对一个在途，和原厂模板一样），不是 1 ms 周期 |
| 决策用的 RTT | 1/4 EWMA 平滑值（`0xcd1 <0\|1> 7` 可切回原始样本）。原因：一 RTT 一个样本，本 fabric 负载下探针抖动 sd 1–2 µs、底噪 2.7 µs，用原始样本时 6 对场景每个尾巴尖峰都触发一次减速，口只到 83%、平均 RTT 反而比目标低 3 µs |
| 邮箱 | `0xccd 3` 选 Swift（切换时清所有流对的 CC 状态）；`0xcd1 <值> <项>` 在线调参（0 base_target ns、1 fs_range ns、2 α ns、3 β ns、4 ai 字节、5 b fxp16、6 max_mdf fxp16、7 用平滑 RTT）；`0xcd0 1` 自动把未登记的 flowtag 建成 budget=MAX 的流对（motivation 用，"HyperFront 关掉"的形态，不需要 agent）；`0xdee <pair>` 回读 {flowtag, cwnd, rtt_s, target, cc_rate, 减速次数, 丢包减速次数, qp_count, rtt_last, qp_nslot, rtt_n}；`0xdef` 按事件类型回读 QPN 变化计数（诊断） |
| 顺带修的执行面缺口 | ① QP 映射表原来只用 QPN 做键，QPN 是每个 VF 各自编号的，同一发送端两个 VF 的 QP 撞键，vf3 的事件记到 vf1 的流对上（qp_count 读 0–3、速率发错流对）；改为 (QPN, flowtag) 联合键。② RTT 事件带的 QPN 每个事件都不一样（84 万事件、85 万次变化），会把每流对 64 个槽位撑满，真 QP 挤不进去；现在只从 ROCE_TX 事件学 QP。③ 流对删除后重建落到别的下标时，映射表仍指旧下标；现在查表命中时核对 flowtag，不符就重学。这三处对生产形态（有 qpn_resolver、每发送端一个 VF）应无影响，但**一个发送端多个 VF 同时发的实验**以前的 qp_count 可能就不准，值得回头核一下 |

编译：`tools/lab-infra/deploy_check.sh --deploy`（自动 scp + `meson --reconfigure` + ninja，四台）。运行：`results/swift_20260827/setup_senders.sh 3`（三台发送端起 HPFT 执行面、`0xccd 3`、`0xcca 0` min 组合器、`0xccf` 线速、`0xcd0 1`）。

## 二、校准记录（设备时钟，ns）

| 量 | 实测 | 来源 |
|---|---|---|
| 底噪 RTT | **2744**（`rtt_min`） | 单流独占（solo_v1） |
| 单流满 VF 上限（49 G）时的 RTT | 3.7–3.9 µs，sd 0.3–0.4 | solo_v1 的 `rtt_s` |
| 6 对打 200 G 口、AIMD 项（CNP 驱动，队列由交换机 ECN 带 Kmin 400 KB/Kmax 1.6 MB 定）时的 RTT | 均值 27–37 µs，峰 50–64 µs | port6_aimd 的 `rtt_last` |
| TCP（4×10 条 cubic）把队列填满时的 RTT | 2.1–2.3 ms | tcpmix_v1 的 `rtt_s` |

由此定的参数（也是代码默认值）：

| 参数 | 值 | 依据 |
|---|---|---|
| base_target | 6.0 µs | ≈ 2.2× 底噪，高于单流满载抖动带（3.7–3.9 ± 0.4） |
| fs_range | 20 µs | 小窗口流允许多占的时延，落在 ECN 带（16 µs @400 KB）附近、远低于 AIMD 拥塞态 |
| fs_min / fs_max cwnd | 4 / 100 包（1 包 = 1024 B） | 100 包 ≈ 200 G × 4 µs 的 BDP；由此 α = 50 µs、β = 5 µs |
| ai | 1024 B / RTT | 论文的 1 包/RTT |
| b, max_mdf | 0.8, 0.5 | 论文默认 |
| cwnd 范围 | 1 KB – (1 MB − 1 KB) | 1 MB ≈ 线速 × 40 µs；上限必须 < 2^20，否则 `cwnd<<12` 溢出（第一版就踩了：cwnd 到 1 MB 速率归零） |

## 三、环境表（三个验证子实验共用）

| 项 | 取值 |
|---|---|
| 机器 | sgpu01/03/04 发送、sgpu02 接收，各 8 VF，每 VF 50 G（发送端 devlink tx_max + 接收端 OVS meter，`vf_caps.sh sync` 后 8/8） |
| UPCC / RDMA CC | 四台 UPCC=1；三台发送端 DPU 跑 HPFT 执行面 `0xccd 3`（Swift）；接收端 DPU 跑同一执行面只作探针应答方，不登记流对 |
| HyperFront | 关：无 rx/tx agent、无 shim、无 qpn_resolver；`0xcd0 1` 自动登记流对 budget=MAX；`0xcca 0`（速率 = min(cc, level)，level=MAX/N 不束缚）；`0xccf` 线速 |
| RDMA 重传 | GBN（`cc_mode.sh gbn`，四台 SR=0） |
| MTU | VF 1500（四台核对）；RoCE 路径 `-m 1024` 两端 |
| 交换机 | `swp37s0` ECN `motiv_default_ecn`（status 核对）；无类分队列、无 PFC（沿用 1-2_20260826 的设置，本轮未重新登录交换机核对） |
| TCP | iperf3 3.20 `-C cubic`、`--start-at`；不开 ECN（沿用 1-2 的主机 sysctl，本轮未复核） |
| 打流器 | `perftest-26015/ib_write_bw -x 3 -m 1024 -q 10 --start_at`，四台同一份 |
| 测量 | 接收端 DPU vport meter（100 ms，按目的 VF 分桶，`rx_ib`/`rx_eth`）；发送端 DPU `0xdee` 轮询（250 ms，每对）；两端 CNP/ECN 计数；perftest 自报只作旁证 |
| 时长 | 5 s 预热 + 30 s 计时；图和表只取计时后 0–30 s；各 1 遍 |

## 四、子实验

### 4.1 单流独占（solo_v1）

| 行 | src | dst | 类型 | 数量 | 起 | 止 |
|---|---|---|---|---|---|---|
| 1 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |

预期：跑满 VF 上限 ~49 G 且稳定。实测：接收 **48.97 / 48.98 / 48.71 G**（三个 10 s 窗）；cwnd 停在上限 ~1 MB，`rtt_s` 3.7–3.9 µs（sd 0.3），每 10 s 只有 ~270 次减速（抖动尾巴），无丢包减速；速率项 = MAX，束缚的是 VF 上限。**正向，信心高**。图 `fig_solo_v1.png`。

### 4.2 六对打一个 200 G 口（port6_swift_v2；同表跑 AIMD 项作校准参照 port6_aimd）

| 行 | src | dst | 类型 | 数量 | 起 | 止 |
|---|---|---|---|---|---|---|
| 1 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 2 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 3 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 4 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf5 (10.1.5.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 5 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf6 (10.1.6.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 6 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf7 (10.1.7.2) | RDMA WRITE | 10 QP | 0 | 30 |

需求 6 × 50 = 300 G > 口的 190.7 G，队列只在交换机口上。预期：六对均分（各 ~32 G）、RTT 停在 target 附近。实测（30 s 均值，Gb/s）：

| 项 | vf2 | vf3 | vf4 | vf5 | vf6 | vf7 | 合计 | Jain |
|---|---|---|---|---|---|---|---|---|
| Swift | 26.2 | 27.1 | 26.5 | 26.1 | 27.5 | 27.4 | **160.8（口的 84%）** | **0.9996** |
| AIMD 项（参照） | 9.9 | 45.6 | 43.2 | 9.8 | 38.4 | 40.5 | 187.4（98%） | 0.80 |

Swift 遥测（每对，5–30 s）：cwnd 27–30 KB（sd ~5），`rtt_s` 8.5–9.7 µs（sd 2.1–2.6）对 target 10.3–10.9 µs，每对每秒 ~4.3k 次减速、0 次丢包减速，qp_count 稳定 10；30 s 里接收端 ECN 标记只增加 106 万（AIMD 参照 7200 万）——队列 ≈ (8.9 − 2.7) µs × 25 B/ns ≈ 150 KB，低于 Kmin。**份额与队列两条都正向、信心高；口利用率 84% 是负向项**：Swift 的锯齿（+1 包/RTT、按超出比例减）在这么短的 RTT 下每 ~2 RTT 就动一次，平均落在目标之下，属于算法本性而非实现 bug；要拉高可以调 base_target/ai，但那是另一轮的事。图 `fig_port6_swift_v2.png`、`fig_port6_aimd.png`。

### 4.3 四对 incast 到一个 VF（incast4_v1）

| 行 | src | dst | 类型 | 数量 | 起 | 止 |
|---|---|---|---|---|---|---|
| 1 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 2 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 3 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 4 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |

预期（任务书）：各 12.5 G、RTT 停在 target。实测：接收端 vport 计数 50.7 G（这是 meter 之前的到达量）；perftest goodput **3.27 / 3.28 / 4.42 / 4.23 G，合计 15.2 G**（Jain 0.98）；每对每 10 s ~19.5k 次丢包减速、几乎没有时延减速；`rtt_s` 4.5–5 µs（无队列）。**判读：这个场景的瓶颈是接收端 DPU 的 OVS drop-band meter——它丢包、不排队，Swift 拿不到时延信号，只能靠 NACK 每 RTT 减半，再用 +1 包/RTT 爬回，所以 goodput 只有上限的 30%。份额是均的，但吞吐是负向的**；这不是 Swift 实现的问题，任何纯时延 CC 在丢包型限速器前都这样。要测"incast 到一个目的、队列停在 target"，得让瓶颈是交换机口（4.2 已经证明），或者把接收端 VF 限速改成整形（排队）而不是 policer。信心高。图 `fig_incast4_v1.png`。

### 4.4 与 TCP 同队列（tcpmix_v1，1-2 打流表原样，TCP cubic，GBN）

| 行 | src | dst | 类型 | 数量 | 起 | 止 |
|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 cubic | 10 流 | 10 | 20 |
| 2 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 cubic | 10 流 | 10 | 20 |
| 3 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 cubic | 10 流 | 10 | 20 |
| 4 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 cubic | 10 流 | 10 | 20 |
| 5 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 6 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf5 (10.1.5.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 7 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf6 (10.1.6.2) | RDMA WRITE | 10 QP | 0 | 30 |
| 8 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf7 (10.1.7.2) | RDMA WRITE | 10 QP | 0 | 30 |

预期：TCP 一到 RDMA 被压到 ~1%。实测（接收端，Gb/s）：

| 类 | 0–10 s | 10–20 s 稳态（11–19.5 s） | 20–30 s |
|---|---|---|---|
| RDMA 4 租户合计 | 167.1（各 41–43） | **0.49（各 0.12；口的 0.26%）** | 165.8 |
| TCP 4 租户合计 | — | 190.3（各 44–48） | — |

边沿（100 ms 桶，RDMA 合计）：10.0 s 167 → 10.1 s 157 → 10.2 s 48 → 10.3 s 5 → 10.4 s 起 0.5；TCP 走后 20.0 s 79 → 20.1 s 167（**0.1–0.2 s 恢复**，因为 Swift 一个 RTT 就能从 1 KB 加回来、无 DCQCN 那种分级恢复）。遥测：TCP 在场时探针 RTT 2.1–2.3 ms，cwnd 钉在下限 1 KB，速率项在 12 Mb/s/QP 的地板上；TCP 有 953–3466 次重传（只认丢包）。对照 1-2_20260826：DCQCN 行稳态 ~0.8 G、ZTR 行更低——Swift 与它们同一形态，且被压得最深。**正向（作为 motivation 论据），信心高**：单遍，边沿干净。图 `fig_tcpmix_v1.png`。

另：RDMA 独占期 167 G = 口的 88%，低于 DCQCN 行的 187 G，同 4.2 的利用率注记。

## 五、可写进论文的算法与校准说明

我们在 BlueField-3 的可编程拥塞控制框架（DOCA PCC）上实现了 Swift [SIGCOMM'20] 作为第三种 RDMA 拥塞控制。硬件只接受速率，所以窗口按 rate = cwnd / RTT 换算后下发；每条流保持一个在途 RTT 探针，探针返回即为一次控制步。目标时延 target = base_target + clamp(α/√cwnd − β, 0, fs_range)，RTT 低于目标时每 RTT 加一包，高于目标且距上次减速已过一个 RTT 时按超出比例乘性减（最多减半），丢包（NACK）直接减半；ECN 标记不参与。RDMA 路径上没有主机侧的 endpoint delay，只用了 Swift 的 fabric delay 一半。阈值按本 fabric 实测校准而非沿用论文：探针底噪 RTT 2.7 µs，单流满载抖动带 3.7–3.9 µs，交换机 ECN 起标点（400 KB）对应 16 µs，CNP 驱动的拥塞态 RTT 27–37 µs；据此取 base_target = 6 µs、fs_range = 20 µs、fs 缩放区间 4–100 包。因为一个 RTT 只有一个探针样本、且硬件探针抖动 sd 1–2 µs 与底噪同量级，决策用 1/4 EWMA 平滑后的 RTT。校验：单流跑到 VF 上限；六条流汇聚到一个 200 G 口时份额 Jain 0.9996、队列 ≈150 KB（RTT 8.9 µs 对目标 10.4 µs）、几乎无 ECN 标记。

## 六、目录

`run.sh <tag> <场景>`（solo/incast4/port6/tcpmix，`ALGO=` 选 CC 项、`TCPCC=` 选 TCP CC）、`setup_senders.sh`、`mb.sh`（邮箱）、`swift_poll.sh`（DPU 侧遥测轮询）、`parse_poll.py`、`plot.py <tag>`；原始数据在 `results/<tag>/`（vpm_series.csv、poll_<dpu>.log、flow*.log、cnp_pre/post）。`results/diag_*` 与 `port6_swift_v0/v1`、`solo_v0` 是修 bug 过程中的中间数据，只作追溯。

## 七、lab 现状

**停在 UPCC=1 + 三台发送端 HPFT 执行面（Swift）+ 接收端应答方**，agent 全停，GBN，8 VF/50 G，交叉对规则已 apply。回 plain 要 `lab_env.sh plain`（fw reset ~6 min）+ `vf_caps.sh sync` + `cc_mode.sh gbn|sr` + 四台 `cross_pair_net.sh apply` + MTU 核对。

## 六、利用率 84–88% 的原因与最终形态（2026-08-27 验收后补）

原因：执行面版把**一个流对（10 个 QP）当作一条 Swift 流**——cwnd 是流对级的，速率按 QP 数均摊，等于每个 QP 只拿到 1/10 的加性增；论文里每条连接就是一条流。在线调参证实了方向：加性增加大反而更差（4 包 143 G、10 包 128 G，上冲后砍得更深），把砍幅收小、目标抬高只能到 176 G（92%）：

| 参数（执行面版，4 对 RDMA 打口，4–14 s 均值） | RDMA 合计 |
|---|---|
| base 6 µs, ai 1 包, b 0.8, mdf 0.5（原默认） | 167 G（88%） |
| base 12 µs, ai 1 包, b 0.8, mdf 0.5 | 172.5 G（90%） |
| base 12 µs, ai 1 包, b 0.4, mdf 0.25 | 174.4 G（91%） |
| base 16 µs, ai 1 包, b 0.4, mdf 0.25 | 176.4 G（92%） |
| base 6 µs, ai 4 / 10 包 | 143 / 128 G |

最终形态：**独立的原厂模板二进制** `~/bzx/pcc_swift_stock`（`tools/dpu/pcc/swift_stock/`），Swift 写在 `algorithm_core` 位置，每个 PCC 流上下文（= 每个 QP）一条 Swift 流，参数 base 12 µs / fs_range 20 µs / ai 1 包 / b 0.4 / mdf 0.25。同一 4 对场景 **187.4 G（98%，两遍 187.4 / 187.4，sd 0.5）**，与 DCQCN 行持平。motivation 1-2 的 Swift 行用的就是这个二进制（`lab_env.sh swift`），不含任何 HyperFront 代码；执行面里的 `0xccd 3` 保留作 HPFT 内部对照。
