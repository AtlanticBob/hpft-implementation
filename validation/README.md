# HyperFront 控制设计验证基准（validation，2026-08-28 起草，待确认）

这个目录定义一组固定的实验，用来回答"某一版控制设计有没有效"。它的写法照搬 `hpft-paper/paper/1-x` 三个 motivation 包：每个场景先写清楚拓扑、环境表、打流表（一行一对、写明 IP、不用"同上"）、权重、预期，然后才有结果。所有场景共用一套环境；换一版设计（`e_params.law` 或参数）时环境不变，只换被测对象，所以不同版本的结果可以直接摆在一起比。

之前 `tools/tests/` 下的 `incast8_regression.sh` 和 `step_regression.sh` 是开发过程中的回归脚本，配置写在脚本里没有单独说明，这个目录建成之后它们不再作为判断依据。

## 一、七个场景各回答什么问题

| 编号 | 名字 | 回答的问题 |
|---|---|---|
| V1 | 稳态 incast | 三台发送端把接收端口打满时，24 个流集合是否按政策分得平均、端口是否用满、队列是否为零 |
| V2 | 流集合加入/退出 | 同一个目的 VM 上来了第二个发送方，在位流让出一半、退出后收回，各用多久，会不会塌 |
| V3 | 租户加入/退出 | 新的目的 VM（租户）加入，根层份额在租户之间重划，各用多久 |
| V4 | 需求变化与借用 | 一条流应用需求只有 10 G 时兄弟类能不能借到剩下的，需求涨回来时能不能在一秒内要回自己的份额（接收端能不能分清"不想发"和"被压着"） |
| V5 | 单目的额度、零物理拥塞 | 瓶颈只是政策额度、网络里没有任何排队和丢包时，三个发送方是否仍按份额分，信号是不是接收端自己合成的 |
| V6 | 换一种拥塞控制 | 把 RDMA 换成 Swift、TCP 换成 BBR 之后重跑 V2，结果是否不变（设计不依赖具体 CC） |
| V7 | 与租户 CC 共处 | 接收端口被 HyperFront 看不见的 40 G UDP 压住时，租户 CC 能不能接管、有没有流被饿死、背景停后能不能回到份额 |

V1、V2、V4、V7 是每一版设计必跑的四个；V3、V5、V6 在它们过了之后跑。

## 二、共同环境（除非某个场景明确覆盖）

三台发送端 sgpu01/03/04 经 sn5600 打接收端 sgpu02，四台的 p1 都是 200 G，接收端口 `swp37s0` 是 3:1 超订的瓶颈。数据面是常驻 VxLAN overlay 星型（中心 sgpu02，`tos=inherit`）。租户 = sgpu02 的一个 VF；流集合 = (源 VM, 目的 VM, 类)。

| 项 | 取值 |
|---|---|
| 主机与 DPU | sgpu01/hpft-dpu、sgpu02/hpft-dpu2（接收）、sgpu03/hpft-dpu3、sgpu04/hpft-dpu4 |
| 每 VF 带宽上限 | 每台 8 个 VF、每个 VF 50 G（发送端 devlink tx_max + 接收端 OVS drop meter，`vf_caps.sh sync`），全部保持 |
| HyperFront | 四台都在 `lab_env.sh hpft` 态：UPCC=1，四台 DPU 跑本仓库的 PCC 执行面；`roles.sh set --receiver sgpu02 --senders sgpu01,sgpu03,sgpu04`；TCP 执行面 host fq+EDT，每个 VF 恰好挂一份当前程序 |
| 被测对象 | `config/lab-registry.json` 的 `e_params`（law 与参数），每次运行原样记入报告 |
| 政策 | 每个 VM 权重 1、`max_rate_bps` 50 G、类权重 tcp:rdma = 1:1、per-sender 权重全 1；headroom 8% 只作用在根上：根容量 C′ = 200 × 0.92 = 184 G，每 VM 上限就是 50 G（四个 VM 同时满发时根先绑定，各得 46 G） |
| RDMA 拥塞控制 | PCC 执行面里的 DCQCN 风味项（`0xccd 0`），只读速率不改 CC；V6 换 Swift 项（`0xccd 3`） |
| RDMA 重传 | SR（`cc_mode.sh sr`，四台 host；fw reset 后要重设）。当前 lab 是 GBN，跑前要切 |
| TCP 拥塞控制 | Cubic，不开 ECN（`tcp_ecn=2`）；V6 换 BBR |
| MTU | VF 1500，p1 9000；RoCE 路径 MTU 1024（perftest 两端 `-m 1024`） |
| 交换机 ECN | `swp37s0` 绑 `motiv_default_ecn`（TC0/TC3 Kmin 400 KB / Kmax 1.6 MB / Pmax 20%）；预期 V1–V5 几乎不产生标记，标记数记入报告作旁证 |
| 交换机流量类 | 不用：RDMA 与 TCP 同在 TC0，`swp37s0` 无 egress-scheduler |
| 交换机 PFC 与 pause | lossy：四个 host 口绑 `motiv-nopfc`，pause 关 |
| 打流器 | RDMA `~/hyperfront/perftest-26015/ib_write_bw`（带 `--start_at`，四台同一份），`-q 4 -m 1024 --report_gbits -D`；TCP `tools/host/tcp_blast.c`（运行时在各主机现编到 /tmp），`-P 4 -S <绝对时刻> -B <ip> -I dpu1vfN` |
| 限速的 RDMA 行 | 两端都加 `-s 8192`。硬件限速被 QP 拒绝（PCC 执行面占着 QP 的速率），packet pacing 只支持 Raw Ethernet，所以只能用软件限速，而它按 `burst_size`（默认等于 tx_depth 128）条消息成批发送再忙等。默认的 64 KB 消息下一批 8.4 MB、每 6.71 ms 一次，20 ms 的遥测采样窗里只装得下三批，量化出 ±33% 的假抖动，任何事件都不可能在 ±10% 的带里待满一秒；8 KB 消息把批间隔压到 0.84 ms，实测归因速率的标准差从 17% 降到 2%。**不要改用调小 `burst_size` 的办法**：在途消息数掉到 8 时，新流填不满还在地板上的围栏，围栏的上限（自身用量的两倍）因此升不上去，整段只发出 0.26 G（r23）|
| 起步纪律 | RDMA 不加 `--rate_limit`（V4 的需求限制除外）：HyperFront 执行面的未知流上限（每 QP 5 G）就是起步纪律，QP 被打死算一次失败 |
| 时长与计时 | V1 20 s、V2 三段各 10 s；其余场景仍是 90 s（V5 为 60 s），未改。t=0 的流先预热 5 s 再开始计时；晚加入的流两类都按绝对时刻准点加入——RDMA 用 perftest `--start_at`，TCP 用 tcp_blast `-S`。**iperf3 不能这样用**：它没有绝对或延迟起始，runner 只能睡到时刻再 exec，于是它 0.5–1.1 s 的启动全落在事件之后，接收端在那段时间里一个字节都收不到（2026-08-31 实测）。图和表只取计时后的部分 |
| 重复 | 每个场景每版设计跑 1 遍出结论；作为定版依据的场景跑 3 遍，收敛时间报 min/median/max |

## 三、测量与判据

**测什么。** 三路数据同时采：接收端 DPU 的 vport 硬件计数（`hpft-vport-meter`，按目的 VF 分桶、RDMA/TCP 分开，100 ms 采样，wire 口径）是每个 VM 每类拿到多少的真值；每条流的应用自报 goodput（perftest BW average、tcp_blast 汇总行）是每个流集合拿到多少的依据（硬件计数分不出同一 VM 上的两个发送方）；agent 遥测 jsonl（接收端每个流集合的到达 A、入场额 E、虚拟队列 q、γ；发送端的 R、Ê，20 ms 一条）用来看控制面的变量。跑完再抓一次执行面的信任度（TCP 读 BPF `hpft_pair_state`，RDMA 走设备查询 `0xded`）、交换机 ECN 标记计数、双端 CNP 计数。

**怎么判。** 每个场景的"应得"由政策算出来写在预期里。判据全部用同一套：

1. 稳态贴合度：每个流集合的 goodput 与应得之差 ≤ 5%，同组流集合的 Jain 指数 ≥ 0.99；根饱和时合计 ≥ 95% C′。
2. 队列消得掉：稳态里虚拟队列的平均 ≤ 1 ms，且每个流集合至少每秒把账清到零一次。设计要求这本账不积压，检验的就该是它清不清得掉；按"非空时刻的比例"打分不合适，因为这套控制的稳态本来就是锯齿，队列周期性出现是构造使然，要求 95% 的时刻低于某个值等价于要求围栏 95% 的时间待在份额之下，那是拿带宽去换一本好看的账（2026-08-30 改）。
3. 收敛：从事件时刻起，到目标流集合的速率进入新应得的 ±10% 并保持 ≥ 1 s 的首个时刻，≤ 1 s 为合格（沿用 `evaluation_plan.md` §1.5 的定义）。
4. 不塌：除事件后 2 s 的瞬态外，任何流集合的速率不得低于应得的 50% 持续 1 s 以上；合计不得低于 90% C′ 持续 1 s 以上。
5. 平台健康：所有流正常结束、没有 QP 错误完成；四台 `deploy_check.sh` 跑前通过。

不满足任何一条就是这一版设计在这个场景上不过，报告里写明是哪一条、数字是多少。

## 四、场景定义

### V1 稳态 incast（20 s）

三台发送端各 4 个 VM、每个 VM 两类，24 个流集合同时打 sgpu02 的 4 个 VM。四个 VM 各应得 184/4 = 46 G（恰好等于每 VM 上限），每个 VM 上 6 个流集合各应得 46/6 = 7.67 G。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 17 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 18 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 19 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 20 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 21 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 22 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 23 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 24 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |

**预期。** 计时后 5–60 s 每个流集合 7.67 G ± 5%，Jain ≥ 0.99，合计 ≥ 175 G，虚拟队列为零。交换机 ECN 标记数记入报告：端口跑在 C′ 的 98% 时微突发会让队列偶尔过 Kmin，实测约 1% 的 RoCE 包被标记（2026-08-28 首轮），这是环境事实不是判据。**图**：每 VM 每类的 wire 时序（堆叠面积，8 段）+ 24 个流集合的 goodput 柱。

### V2 流集合加入/退出（30 s）

sgpu01 的 4 个 VM 两类共 8 个流集合常驻；sgpu03 的 4 个 VM 两类共 8 个流集合在 10 s 加入、20 s 退出，每个加入者与一个在位者共用同一个目的 VM。每个目的 VM 的 46 G 在 0–10 s 由 2 个流集合分（各 23 G），10–20 s 由 4 个分（各 11.5 G），20–30 s 回到各 23 G。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 30 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 30 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 30 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 30 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 30 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 30 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 30 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 30 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 10 | 20 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 10 | 20 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 10 | 20 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 10 | 20 |  |

**预期。** 在位者 23 → 11.5 → 23 G，加入者从零到 11.5 G，四个阶跃的收敛时间都 ≤ 1 s；加入瞬间合计不超过 110% C′；整场没有塌陷；20–30 s 与 0–10 s 的数字一致。**图**：每流集合速率时序 + 虚拟队列 + 合计，事件竖线与应得参考线。

### V3 租户加入/退出（90 s）

sgpu01 打 sgpu02 的 vf0–vf3 四个租户常驻，四个租户各拿 46 G 把 184 G 的根恰好用满；sgpu03 在 30 s 给 vf4–vf7 四个新租户各打两类，60 s 退出。根层按租户权重重划：每个租户 46 → 23 → 46 G，租户内每类 23 → 11.5 → 23 G。与 V2 的区别是重划发生在根层（租户之间），不是同一个 VM 内。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 9 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 10 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | TCP tcp_blast | 4 流 | 30 | 60 |  |
| 11 | sgpu03/vf5 (10.1.5.3) | sgpu02/vf5 (10.1.5.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 12 | sgpu03/vf5 (10.1.5.3) | sgpu02/vf5 (10.1.5.2) | TCP tcp_blast | 4 流 | 30 | 60 |  |
| 13 | sgpu03/vf6 (10.1.6.3) | sgpu02/vf6 (10.1.6.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 14 | sgpu03/vf6 (10.1.6.3) | sgpu02/vf6 (10.1.6.2) | TCP tcp_blast | 4 流 | 30 | 60 |  |
| 15 | sgpu03/vf7 (10.1.7.3) | sgpu02/vf7 (10.1.7.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 16 | sgpu03/vf7 (10.1.7.3) | sgpu02/vf7 (10.1.7.2) | TCP tcp_blast | 4 流 | 30 | 60 |  |

**预期。** 每个在位租户 46 → 23 → 46 G，每个新租户 0 → 23 → 0 G，收敛 ≤ 1 s，无塌陷。**图**：每租户 wire 时序（堆叠面积，8 段）+ 收敛时间表。

### V4 需求变化与借用（90 s，单租户）

只有一个租户 sgpu02/vf0，上限 50 G，两类各应得 25 G。TCP 全程满发；RDMA 的应用需求分三段：0–30 s 只要 10 G（1 个 QP 加 `--rate_limit 10`），30–60 s 满发（4 个 QP），60–90 s 又只要 10 G。三段是三个 perftest 实例，同一个流集合。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 1 QP | 0 | 30 | `--rate_limit 10`（应用需求 10 G） |
| 3 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 4 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 1 QP | 60 | 90 | `--rate_limit 10`（应用需求 10 G） |

**预期。** 0–30 s：RDMA 10 G（应用口径；线上 10.7 G，1024 B 报文的头部开销 7.3%），接收端给它留 15% 的增长余量（线上 12.3 G），TCP 借到其余 37.7 G。30 s：RDMA 需求涨起来，1 s 内回到 25 G，TCP 退到 25 G。60 s：RDMA 回落到 10 G，TCP 1 s 内又借到 37.7 G。全程虚拟队列为零、合计 ≈ 50 G。这是"接收端把发得少当成不想发"那类问题的直接检验：如果 30 s 之后 RDMA 只能按每周期一小步往上爬，这里会看到。**图**：两个流集合的速率时序 + 应得参考线。

### V5 单目的额度、零物理拥塞（60 s）

三个发送方各从自己的 vf0 打 sgpu02/vf0 同一个 VM，每方两类共 6 个流集合。VM 上限 50 G，三个发送方各应得 16.67 G，每个流集合 8.33 G。链路是 200 G、三个源 VF 的 50 G 上限之和 150 G 也不到链路，所以网络里没有任何物理排队，稀缺性只来自政策额度，信号只能是接收端自己合成的。三个发送方用的都是各自主机的 vf0，不同 DPU 上的同索引 VF 不会发生 flowtag 碰撞。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 3 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 4 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |
| 5 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 6 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 20 |  |

**预期。** 6 个流集合各 8.33 G ± 5%，合计 ≈ 50 G；交换机 ECN 标记 = 0、CNP = 0（证明没有物理拥塞）；接收端虚拟队列在稳态偶有非空但平均 ≤ 1 ms。**图**：6 个流集合的 goodput 柱 + 机制旁证表。

### V6 换一种拥塞控制（90 s）

打流与 V2 完全相同，只把 RDMA 的 CC 换成执行面里的 Swift 项（`0xccd 3`，阈值按 `results/swift_20260827/` 的校准），TCP 换成 BBR（`iperf3 -C bbr`）。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 | 执行面 Swift 项（`0xccd 3`） |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 | iperf3 `-C bbr` |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 | 执行面 Swift 项（`0xccd 3`） |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 90 | iperf3 `-C bbr` |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 | 执行面 Swift 项（`0xccd 3`） |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 90 | iperf3 `-C bbr` |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 | 执行面 Swift 项（`0xccd 3`） |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 90 | iperf3 `-C bbr` |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 30 | 60 | 执行面 Swift 项（`0xccd 3`） |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 30 | 60 | iperf3 `-C bbr` |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 30 | 60 | 执行面 Swift 项（`0xccd 3`） |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 30 | 60 | iperf3 `-C bbr` |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 30 | 60 | 执行面 Swift 项（`0xccd 3`） |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 30 | 60 | iperf3 `-C bbr` |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 30 | 60 | 执行面 Swift 项（`0xccd 3`） |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 30 | 60 | iperf3 `-C bbr` |

**预期。** 与 V2 相同：23 → 11.5 → 23 G，收敛 ≤ 1 s，无塌陷。差异超过 5% 就说明设计里有依赖具体 CC 的地方。

### V7 与租户 CC 共处：接收端口被 HyperFront 看不见的流量压住（90 s）

打流与 V1 相同（24 个流集合把根用满），另加一股 HyperFront 既不调度也不整形的背景 UDP：30–60 s 从 sgpu04/vf7 往 sgpu02/vf7 打 40 G（接收端归为 ip_other，不进分配；发送端只对 TCP 和 RDMA 整形）。这 30 秒里端口上有 184 + 40 > 200 G 的需求，交换机队列真的积起来：RoCE 包被打 ECN、DCQCN 减速，TCP 丢包减窗。HyperFront 仍按 184 G 发围栏，只有租户 CC 能把流量压进 160 G，执行面应当把方向盘交给 CC；60 s 背景停掉，围栏应当重新接管。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 17 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 18 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 19 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 20 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 21 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 22 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 23 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 24 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | TCP tcp_blast | 4 流 | 0 | 90 |  |
| 25 | sgpu04/vf7 (10.1.7.4) | sgpu02/vf7 (10.1.7.2) | UDP 背景（udp_blast） | — | 30 | 60 | 40 G，HyperFront 不调度也不整形的流量 |

**预期。** 0–30 s 与 V1 相同。30–60 s：接收端把根容量缩到 (200 − 41) × 0.92 = 146 G（它计量得到 UDP，只是不调度），四个 VM 各 36.6 G，两类各 18.3 G，24 个流集合各 6.09 G ± 5%，Jain ≥ 0.99；端口总载荷（租户 + UDP）≈ 187 G，交换机队列不持续积压，RDMA 不被 CC 压死；执行面的信任度不上升（CC 只是短暂减速、随即涨回，按趋势规则算"追赶"）。60 s 背景停掉，根回到 184 G，1 s 内回到 V1 的份额。**图**：每 VM 每类 wire 时序（vf7 的 eth 桶就是背景 UDP）+ 执行面信任度时序（TCP 每 100 ms 读 BPF，RDMA 每秒查设备）。**判据**：与其他场景相同，应得值按物理根 146 G 计算；另加"没有流集合低于 3 G"。**注意**：设备端探针（`rp_probe.py`）每秒对每个流对发一次邮箱查询，每次占用邮箱 13–22 ms，会让 RDMA 执行面的预算推送抖动（无背景流时队列出现 5–15 ms 的毛刺），所以这个场景的 0–30 s 和 60–90 s 不能拿来和 V1 比稳态。

## 五、目录与产出

```
validation/
  README.md            本文：环境、打流表、判据（唯一的定义）
  EXECUTION.md         跑前准备、跑法、跑后复原
  scenarios/           每个场景一个 .flows 打流表（runner 与本文表格的同一来源，render_table.py 把它渲染成表）
  run/                 run.sh <场景> <tag>：准备、准点打流、采集三路数据、跑后快照
  distill.py           results/<tag>/ → data/<tag>_*.csv（每 VM 每类 100 ms 速率、每流集合 goodput、遥测抽样、判据结果）
  plot/                只读 data/、只写 fig/
  data/  fig/          蒸馏数据与图（入 git）
  results/<tag>/       原始数据（不入 git）：vpm 序列、每流日志、agent jsonl、信任度快照、交换机/CNP 计数、当次 registry 副本
  reports/<tag>.md     每次运行的报告：环境表实际取值、被测 e_params、预期 vs 实测、五条判据逐条判定
```

tag 的命名：`<场景>_<law 或设计版本>_<日期>_r<第几遍>`，例如 `V2_conf5_20260829_r1`。
