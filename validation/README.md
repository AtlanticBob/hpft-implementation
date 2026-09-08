# HyperFront 控制设计验证基准（validation）

这个目录定义一组固定的实验，用来回答"这一版设计有没有效"。每个场景先写清楚拓扑、环境表、打流表（一行一对、写明 IP）、权重、预期，然后才有结果；跑法在第六节。所有场景共用一套环境；换一版设计时环境不变，只换被测对象，所以不同版本的结果可以直接摆在一起比。

被测对象是现行设计：接收端账本按周期给每个流集合一个许可速率 $R$（设计第 4、5 节），发送端执行面是每流集合一个令牌桶（设计第 6 节）：桶以 $R$ 注入，流集合里的每条流（RDMA 的每个 QP、TCP 的每条连接）按它自己的拥塞控制此刻要求的速率 $c_i$ 取令牌，桶空了就按取令牌的快慢分，$r_i = c_i\cdot\min(1, R/\sum_j c_j)$。执行面没有参数、没有置信度、不判断拥塞控制可不可信；$R$ 以上归账本，$R$ 以下归租户的拥塞控制。这里的每个场景都在检验这两层合起来的承诺。

## 一、场景各回答什么问题

| 编号 | 名字 | 回答的问题 |
|---|---|---|
| V1 | 稳态 incast | 三台发送端把接收端口打满时，24 个流集合是否按政策分得平均、端口是否用满、账本是否清得掉；换一种 RDMA 拥塞控制（DCQCN、Swift）结果是否相同 |
| V2 | 流集合加入/退出 | 同一个目的 VM 上来了第二个发送方，在位流让出一半、退出后收回，各用多久，会不会塌 |
| V3 | 租户加入/退出 | 新的目的 VM（租户）加入，根层份额在租户之间重划，各用多久 |
| V4 | 需求变化与借用 | 一条流应用需求只有 10 G 时兄弟类能不能借到剩下的，需求涨回来时能不能在一秒内要回自己的份额 |
| V5 | 单目的额度、零物理拥塞 | 瓶颈只是政策额度、网络里没有任何排队和丢包时，三个发送方是否仍按份额分 |
| V6 | 换一种拥塞控制 | 把 RDMA 换成 Swift、TCP 换成 BBR 之后重跑 V2，结果是否不变 |
| V7 | 与不受管流量共处 | 接收端口被 HyperFront 不调度也不整形的 40 G UDP 压住时，租户拥塞控制在 $R$ 以下退让、账本按到达记账，有没有流集合被饿死、背景停后能不能回到份额。这是设计第 8 节的边界，不是论文的主线 |
| V8 | 账本管不到的核心瓶颈 | 两个接收端共用一根它们的账本都看不见的核心链路并把它打满时，桶执行面的结果是否与租户拥塞控制单独运行相同：不饿死、总量不低、流集合之间不比拥塞控制单独时更不公平 |

V1、V2、V4 是每一版设计必跑的三个；V3、V5、V6 在它们过了之后跑；V7、V8 是设计边界的检验，答案是"和拥塞控制单独运行一样"而不是"达到份额"。

**执行面律的消融**（桶对等额封顶 $\min(c_i, R/N)$、对忽略拥塞控制的等分 $R/N$、对拥塞控制单独运行）不在这里重复，runner 支持这些臂（`HPFT_LAW=1|2`、`HPFT_CC_ONLY=1`），定稿的测量在 `results/perqp_executor_20260907/README.md`（quick 运行器，12 秒一场）与设计文档第 6.6 节。

## 二、共同环境（除非某个场景明确覆盖）

四台主机经 sn5600 互打，四台的 p1 都是 200 G，任何一台都能收也能发（`roles.sh all`），每行打流自带目的主机；V1–V7 是三台打 sgpu02，接收端口 `swp37s0` 是 3:1 超订的瓶颈。sn5600 的常设形态是一台交换机（四个主机口都在 VLAN 100，回环口关着）；V8 需要时按 `run/campaign_20260908_b.sh` 里的步骤把它切成两台逻辑交换机，跑完切回：交换机 A（VLAN 101）接 sgpu01（`swp37s1`）和 sgpu03（`swp3s1`），交换机 B（VLAN 102）接 sgpu02（`swp37s0`）和 sgpu04（`swp4s1`），两台之间只有 `swp21`↔`swp25` 这一根线（出口整形 200 G，`split_core_speed.sh 200|400`，无 PFC）。A 侧打 B 侧的流量都过这根线：单接收端时它与接收端口等粗、不绑定；B 侧两台都收时（V8）它是唯一的瓶颈，是接收端账本管不到的核心链路（设计第 8 节）；端口表、配置脚本与备份在 `tools/lab-infra/switch/`。六个口的 QoS 统一（`set_qos.sh`）：ECN 配置 `hpft_ecn`，K_min 800 KB、K_max 3200 KB、P_max 20%；出向队列门限 `alpha_1`，一个拥塞队列最多占 70.47 MB 共享池的一半、35.2 MB。全部流量走 traffic-class 0，RDMA 和 TCP 落在同一个出向队列、共享同一块缓冲。数据面是常驻 VxLAN overlay、静态全网状：每台 DPU 到其它三台各一条隧道，转发规则由注册表生成，`tos=inherit`；底层是 DPU 的 p1（172.16.1.x，MTU 9000）。租户 = 接收主机的一个 VF；流集合 = (源 VM, 目的 VM, 类)。

| 项 | 取值 |
|---|---|
| 主机与 DPU | sgpu01/hpft-dpu（A 侧）、sgpu02/hpft-dpu2（B 侧）、sgpu03/hpft-dpu3（A 侧）、sgpu04/hpft-dpu4（B 侧）；四台都同时跑接收端与发送端两套代理，谁收谁发由打流表决定 |
| 核心链路 | sn5600 内部 `swp21`↔`swp25` 回环，出口整形 200 G（常设；另一档 400 G），无 PFC；A 侧到 B 侧的流量都经它；runner 把当次档位记进 `results/<tag>/core_speed.txt` |
| 每 VF 带宽上限 | 每台 8 个 VF、每个 VF 50 G（发送端 devlink tx_max + 接收端 OVS drop meter，`vf_caps.sh sync`） |
| HyperFront | 四台都在 `lab_env.sh hpft` 态：UPCC=1，四台 DPU 跑本仓库的 PCC 执行面（`tools/dpu/pcc/rp_rtt_template_dev_main.c`）；TCP 执行面 host fq+EDT，每个 VF 恰好挂一份当前程序（`tools/host/edt_reinstall.sh`） |
| 被测对象 | 账本：`config/lab-registry.json` 的 `e_params`，每次运行原样记入报告。执行面：令牌桶（`0xcce 0 22`，缺省）；消融臂 `HPFT_LAW=1`（等额封顶）、`HPFT_LAW=2`（等分）、`HPFT_CC_ONLY=1`（拥塞控制单独，发送端代理停掉、TCP 速率表清空），当次的臂记在 `results/<tag>/arm.txt` |
| 政策 | 每个 VM 权重 1、`max_rate_bps` 50 G、类权重 tcp:rdma = 1:1、per-sender 权重全 1；headroom 8% 只作用在根上：根容量 C′ = 200 × 0.92 = 184 G，每 VM 上限就是 50 G（四个 VM 同时满发时根先绑定，各得 46 G） |
| RDMA 拥塞控制 | 执行面里逐 QP 运行的租户拥塞控制，邮箱 `0xccd` 选：**2 = DCQCN**（固件参数，丢包后重置目标速率）、**3 = Swift**（目标 25 µs，参数见 `tools/dpu/pcc/README.md`）；缺省 2，`HPFT_RDMA_CC_ALGO=3` 或打流表里的 `rdma_cc=swift` 选 Swift。这是 PCC 里 DCQCN 与 Swift 的唯一版本；原厂二进制是固件拥塞控制，不用 |
| RDMA 重传 | SR，所有 HPFT 实验一律如此。机制是 ROCE_ACCL 寄存器 `selective_repeat_forced_en=1`，`cc_mode.sh sr` 秒切，易失（fw reset 或 Arm 重启后归零）。`run.sh` 开跑前逐台读这个寄存器，不是 1 就中止，并把实测值写进 `results/<tag>/retrans_mode.txt`。不要看状态行里的 `SR current=`：那读的是 mlxconfig 的另一个开关，本 lab 永远是 0 |
| TCP 拥塞控制 | Cubic，不开 ECN（`tcp_ecn=2`）；V6 换 BBR |
| MTU | VF 1500，p1 9000；RoCE 路径 MTU 1024（perftest 两端 `-m 1024`）。1024 B 报文的头部开销让 RDMA 净荷 = 内层线上字节 ÷ 1.073；VxLAN 再加 5%，所以 200 G 端口上受管流量的净荷上限约 177 G |
| 交换机 ECN | 六个口都绑 `hpft_ecn`（TC0 K_min 800 KB / K_max 3.2 MB / P_max 20%）；标记数记入报告作旁证，读法见 §三 |
| 交换机 PFC 与 pause | lossy：四个 host 口绑 `motiv-nopfc`，pause 关 |
| 打流器 | RDMA `~/hyperfront/perftest-enhanced/ib_write_bw`（带 `--start_at`，四台同一份二进制），`-q 4 -m 1024 --report_gbits -D`；TCP 系统 iperf3（3.20 + 本地 `--start-at` 补丁，源码在 `~/hyperfront/iperf320`，四台已装），`-P 4 --start-at <绝对时刻> -J -B <ip>%dpu1vfN`。perftest 的控制连接走管理网（目的主机名），不走 VF：连 VF 的 IP 会让那条空闲连接被接收端算作在场的 TCP 流集合 |
| 限速的 RDMA 行 | 两端都加 `-s 8192`。硬件限速被 QP 拒绝（PCC 执行面占着 QP 的速率），只能用 perftest 的软件限速，它按 `burst_size` 条消息成批发送再忙等；64 KB 消息一批 8.4 MB、每 6.71 ms 一次，20 ms 的遥测采样窗只装得下三批、量化出 ±33% 的假抖动；8 KB 消息把批间隔压到 0.84 ms。不要改用调小 `burst_size` 的办法：在途消息数太少时新流填不满起步的许可速率 |
| 起步纪律 | RDMA 不加 `--rate_limit`（V4 的需求限制除外）：第一个 $R$ 到达之前 QP 按起步值放行（设计第 5.4 节，$R_0/$每流集合预期 QP 数），QP 被打死算一次失败。晚加入的行在起点前几秒才启动打流器（TCP 3 s、RDMA 5 s，runner 自动做）：早启动的空闲控制连接会被对端关掉，还会被接收端算作在场的 TCP 流集合 |
| 时长与计时 | V1 20 s、V2 与 V6 五段各 10 s（RDMA 加入退出、TCP 加入退出各占一段）、V3 90 s、V4 90 s、V5 60 s、V7 90 s、V8 60 s。一次事件只动一类流。t=0 的流先预热 5 s 再开始计时；晚加入的流两类都按绝对时刻准点加入（perftest `--start_at`、iperf3 `--start-at`；后者不在 `--help` 里，查 `strings /usr/local/lib/libiperf.so.0 \| grep start-at`）。图和表只取计时后的部分 |
| 重复 | 每个场景每版设计跑 1 遍出结论；作为定版依据的场景跑 3 遍，收敛时间报 min/median/max，图取三遍的逐点平均画成一张 |

## 三、测量与判据

**测什么。** 四路数据同时采（有几个接收端就采几份，文件带主机名）：

1. 接收端 DPU 的 vport 硬件计数（`hpft-vport-meter`，按目的 VF 分桶、RDMA/TCP 分开，100 ms 采样，wire 口径，`vpm_series_<host>.csv`）是每个 VM 每类拿到多少的真值。
2. 每条流的应用自报 goodput（perftest BW average；iperf3 取接收端收到的字节除以客户端的测试时长，服务端自己报的速率把 `--start-at` 的等待算进了分母、会低估三到四成）是每个流集合拿到多少的依据，因为硬件计数分不出同一 VM 上的两个发送方。
3. 接收端 agent 遥测（`rx_<host>.jsonl`，每个流集合的到达、入场额、归因速率 r、虚拟队列 d，20 ms 一条）和发送端 agent 遥测（`tx_<host>.jsonl`，每个流集合的 $R$）是控制面的变量。
4. 发送端 DPU 的 RDMA 执行面回读（`rp_<host>.jsonl`，`tools/dpu/rp_sample.py`，每流集合每秒一次 `0xded`）：$R$、这个流集合各 QP 已整形速率之和、最近一毫秒取过令牌的 QP 的拥塞控制速率之和与个数。这是执行面对自己的记账，用来直接检验设计 6.4 的前两条性质。每次查询占用邮箱 13–22 ms，是一个小扰动而不是被动的探针，`HPFT_NO_RP_SAMPLE=1` 可以关掉。

跑前跑后各抓一次交换机六个口的出向队列计数（`switch_pre/post.txt`：帧数、ECN 标记帧、缓冲丢弃帧，取 `-o json` 的原始值——表格视图会给帧数套上字节单位，照表格读会把丢包量少算三个数量级）和两端网卡的 CNP 计数（`cnp_pre/post.txt`；发送端的 CNP 计数在每秒几十万次时饱和，只能定性）。

**怎么判。** 每个场景的"应得"由政策算出来写在预期里（`distill.py` 的 `expected_at`：根 → VM → 类 → 流集合逐级注水，权重全 1，VM 上限 50 G，限速行按 $(1+\delta)$ 倍自身用量占位、其余借出）。判据全部用同一套，每个接收端各是一个根：

1. **稳态贴合**：每个流集合的归因速率与应得之差 ≤ 5%，同组流集合的 Jain 指数 ≥ 0.99；根饱和时合计 ≥ 95% C′。有不受管流量时另加"没有流集合低于 3 G"。
2. **账本清得掉**：稳态里虚拟队列的平均 ≤ 1 ms，且每个流集合至少每秒把账清到零一次。这套控制的稳态本来就是锯齿，账周期性出现是构造使然，设计承诺的是账能清掉。
3. **收敛**：从事件时刻起，到目标流集合的速率（归因速率的 100 ms 均值）进入新应得的 ±10% 并保持 ≥ 1 s（窗内 90% 的 100 ms 均值在带内）的首个时刻，≤ 1 s 为合格。20 ms 的原始样本是一个 10 ms 的归因窗，四个 QP 的 RDMA 流集合在 11.5 G 时它的散布约 10%，永远进不了 ±10% 的带，而它的 100 ms 均值离目标不到 3%，所以这一条在 100 ms 粒度上判。
4. **不塌**：除事件后 2 s 的瞬态外，任何流集合的速率不得低于应得的 50% 持续 1 s 以上；合计不得低于 90% C′ 持续 1 s 以上。
5. **平台健康**：所有流正常结束、没有 QP 错误完成；四台 `deploy_check.sh` 跑前通过。
6. **执行面守约**（设计 6.4 性质 1、2，只看 RDMA 执行面的回读，稳态窗口内）：在列表上的 QP 全都在取令牌的样本里，已整形速率之和与 R 之比的均值 ≤ 1.03、95 分位 ≤ 1.10（R 本身是 20 ms 的锯齿、已整形滞后它一个事件，单个 1 s 样本会偏几个百分点）；取令牌的 QP 的拥塞控制速率之和 ≥ R 的样本里，已整形之和 ≥ 0.95 R 的 ≥ 90%。拥塞控制单独臂不适用。回读里已整形之和覆盖列表上的每个 QP，而一毫秒内没取过令牌的 QP 保留着上次给它的速率（当时的分母更小、速率更大），四个 QP 的 perftest 每隔几秒会有一秒只有两三个 QP 在取令牌，这是设计 6.4 的粒度边界而不是律的问题，所以性质 1 只在全员取令牌的样本上判。

判据 1–5 判的是两层合起来的承诺，判据 6 判的是执行面自己；V7、V8 这两个边界场景的应得值仍按账本算，判据 1、3 在那里只报数字（README 各节写明），通过与否看该节的专用判据。不满足任何一条就是这一版设计在这个场景上不过，报告里写明是哪一条、数字是多少。

图一律按 100 ms 粒度画（`plot/timeline.py`）：接收端遥测按 20 ms 记录，在几十秒的横轴上画出来的绝大部分是测量粒度而不是交付速率。判据 1、2、4 读原始账本，判据 3 读 100 ms 均值。

## 四、场景定义

### V1 稳态 incast（20 s，DCQCN 与 Swift 各一遍）

三台发送端各 4 个 VM、每个 VM 两类，24 个流集合同时打 sgpu02 的 4 个 VM。四个 VM 各应得 184/4 = 46 G（恰好等于每 VM 上限），每个 VM 上 6 个流集合各应得 46/6 = 7.67 G。跑两遍：RDMA 拥塞控制用 DCQCN（缺省）和 Swift（`HPFT_RDMA_CC_ALGO=3`），结果应当相同。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 17 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 18 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 19 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 20 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 21 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 22 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 20 |  |
| 23 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 20 |  |
| 24 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 20 |  |

**预期。** 计时后 5–19 s 每个流集合 7.67 G ± 5%，Jain ≥ 0.99，合计 ≥ 175 G，账本清得掉。两类之间允许有百分之几的差：TCP 的上探把线上速率顶在许可速率之上一点，RDMA 的拥塞控制停在许可速率之下一点。交换机接收端口的 ECN 标记数记入报告，是环境事实不是判据。**图**：`fig/V1_timeline.png`、`fig/V1_executor.png`（DCQCN）；`fig/V1swift_*.png`（Swift）。

### V2 流集合加入/退出（50 s）

sgpu01 的 4 个 VM 两类共 8 个流集合常驻；sgpu03 的 4 个 VM 在 10 s 加入 RDMA 流集合、20 s 退出，在 30 s 加入 TCP 流集合、40 s 退出，每个加入者与一个在位者共用同一个目的 VM 的同一个类。一次事件只动一类流：真实系统里流集合和租户的加入退出一次只改变一类流，两类同时变还会让两个执行面的起步瞬态叠在一起。每个目的 VM 的 46 G 两类各 23 G；RDMA 那一类在 10–20 s 由 2 个流集合分（各 11.5 G），TCP 那一类在 30–40 s 同样；其余时段各 23 G。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 50 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 50 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 50 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 50 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 50 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 50 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 50 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 50 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 10 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 11 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 12 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 10 | 20 |  |
| 13 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 30 | 40 |  |
| 14 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 30 | 40 |  |
| 15 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 30 | 40 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 30 | 40 |  |

**预期。** 每类各自：在位者 23 → 11.5 → 23 G，加入者从零到 11.5 G，另一类全程不动；四个阶跃的收敛时间都 ≤ 1 s；整场没有塌陷；20–30 s、40–50 s 与 0–10 s 的数字一致。**图**：`fig/V2_timeline.png`、`fig/V2_executor.png`。

### V3 租户加入/退出（90 s）

sgpu01 打 sgpu02 的 vf0–vf3 四个租户常驻，四个租户各拿 46 G 把 184 G 的根恰好用满；sgpu03 给 vf4–vf7 四个新租户打流，一次事件只动一类流：新租户的 RDMA 在 20 s 加入、TCP 在 40 s 加入，TCP 在 60 s 退出、RDMA 在 80 s 退出。根层按租户权重重划：新租户一出现（20 s，只有 RDMA 一类）就拿到 23 G 的租户份额，那 23 G 先全给它的 RDMA；40 s TCP 加入后租户内两类各 11.5 G；60 s TCP 退出 RDMA 回到 23 G；80 s RDMA 退出租户消失。在位租户 46 → 23（20 s）→ 46（80 s）G，租户内每类 23 → 11.5 → 23 G。与 V2 的区别是重划发生在根层（租户之间），不是同一个 VM 内；40 s 与 60 s 两个事件只在新租户内部重划，在位租户不该动。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 9 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 4 QP | 20 | 80 |  |
| 10 | sgpu03/vf5 (10.1.5.3) | sgpu02/vf5 (10.1.5.2) | RDMA WRITE | 4 QP | 20 | 80 |  |
| 11 | sgpu03/vf6 (10.1.6.3) | sgpu02/vf6 (10.1.6.2) | RDMA WRITE | 4 QP | 20 | 80 |  |
| 12 | sgpu03/vf7 (10.1.7.3) | sgpu02/vf7 (10.1.7.2) | RDMA WRITE | 4 QP | 20 | 80 |  |
| 13 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | TCP iperf3 | 4 流 | 40 | 60 |  |
| 14 | sgpu03/vf5 (10.1.5.3) | sgpu02/vf5 (10.1.5.2) | TCP iperf3 | 4 流 | 40 | 60 |  |
| 15 | sgpu03/vf6 (10.1.6.3) | sgpu02/vf6 (10.1.6.2) | TCP iperf3 | 4 流 | 40 | 60 |  |
| 16 | sgpu03/vf7 (10.1.7.3) | sgpu02/vf7 (10.1.7.2) | TCP iperf3 | 4 流 | 40 | 60 |  |

**预期。** 每个在位租户 46 → 23 → 46 G（只在 20 s 与 80 s 动），每个新租户 0 → 23 → 23 → 23 → 0 G（20 s 起 RDMA 独占，40–60 s 两类各 11.5 G），四个事件的收敛都 ≤ 1 s，无塌陷。**图**：`fig/V3_timeline.png`、`fig/V3_executor.png`。

### V4 需求变化与借用（90 s，单租户）

只有一个租户 sgpu02/vf0，上限 50 G，两类各应得 25 G。TCP 全程满发；RDMA 的应用需求分三段：0–30 s 只要 10 G（1 个 QP 加 `--rate_limit 10`），30–60 s 满发（4 个 QP），60–90 s 又只要 10 G。三段是三个 perftest 实例，同一个流集合。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 1 QP | 0 | 30 | `--rate_limit 10`（应用需求 10 G） |
| 3 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 30 | 60 |  |
| 4 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 1 QP | 60 | 90 | `--rate_limit 10`（应用需求 10 G） |

**预期。** 0–30 s：RDMA 10 G（应用口径；线上 10.7 G），接收端给它留 15% 的增长余量（线上 12.3 G），TCP 借到其余 37.7 G。30 s：RDMA 需求涨起来，1 s 内回到 25 G，TCP 退到 25 G。60 s：RDMA 回落到 10 G，TCP 1 s 内又借到 37.7 G。全程账本清得掉、合计 ≈ 50 G。这是"接收端把发得少当成不想发"那类问题的直接检验：如果 30 s 之后 RDMA 只能一小步一小步往上爬，这里会看到。**图**：`fig/V4_timeline.png`、`fig/V4_executor.png`。

### V5 单目的额度、零物理拥塞（60 s）

三个发送方各从自己的 vf0 打 sgpu02/vf0 同一个 VM，每方两类共 6 个流集合。VM 上限 50 G，三个发送方各应得 16.67 G，每个流集合 8.33 G。链路是 200 G、三个源 VF 的 50 G 上限之和 150 G 也不到链路，所以网络里没有任何物理排队，稀缺性只来自政策额度，信号只能是接收端自己合成的。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 60 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 60 |  |
| 3 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 60 |  |
| 4 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 60 |  |
| 5 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 60 |  |
| 6 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 60 |  |

**预期。** 6 个流集合各 8.33 G ± 5%，合计 ≈ 50 G；交换机 ECN 标记与缓冲丢弃都为零、CNP 为零（证明没有物理拥塞）；接收端虚拟队列在稳态偶有非空但平均 ≤ 1 ms。**图**：`fig/V5_timeline.png`、`fig/V5_executor.png`。

### V6 换一种拥塞控制（50 s）

打流与 V2 完全相同（RDMA 10 s 加入、20 s 退出；TCP 30 s 加入、40 s 退出），只把 RDMA 的拥塞控制换成执行面里的 Swift（`0xccd 3`，runner 看到 `rdma_cc=swift` 自动选），TCP 换成 BBR（`iperf3 -C bbr`）。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 50 | 执行面 Swift 项（`0xccd 3`） |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 50 | iperf3 `-C bbr` |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 50 | 执行面 Swift 项（`0xccd 3`） |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 50 | iperf3 `-C bbr` |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 50 | 执行面 Swift 项（`0xccd 3`） |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 50 | iperf3 `-C bbr` |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 50 | 执行面 Swift 项（`0xccd 3`） |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 50 | iperf3 `-C bbr` |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 10 | 20 | 执行面 Swift 项（`0xccd 3`） |
| 10 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 10 | 20 | 执行面 Swift 项（`0xccd 3`） |
| 11 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 10 | 20 | 执行面 Swift 项（`0xccd 3`） |
| 12 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 10 | 20 | 执行面 Swift 项（`0xccd 3`） |
| 13 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 30 | 40 | iperf3 `-C bbr` |
| 14 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 30 | 40 | iperf3 `-C bbr` |
| 15 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 30 | 40 | iperf3 `-C bbr` |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 30 | 40 | iperf3 `-C bbr` |

**预期。** 与 V2 相同：每类各自 23 → 11.5 → 23 G，收敛 ≤ 1 s，无塌陷。差异超过 5% 就说明设计里有依赖具体拥塞控制的地方。**图**：`fig/V6_timeline.png`、`fig/V6_executor.png`。

### V7 与不受管流量共处：接收端口被 HyperFront 看不见的流量压住（90 s，边界）

打流与 V1 相同（24 个流集合把根用满），另加一股 HyperFront 既不调度也不整形的背景 UDP：30–60 s 从 sgpu04/vf7 往 sgpu02/vf7 打 40 G（接收端归为 ip_other，不进分配；发送端只对 TCP 和 RDMA 整形）。这 30 秒里端口上有 184 + 40 > 200 G 的需求，交换机队列真的积起来：RoCE 包被打 ECN、DCQCN 减速，TCP 丢包减窗。账本把根缩到 (200 − 41) × 0.92 = 146 G 发许可速率，桶让拥塞控制在许可速率之下退让，退让在账本的容差 $\delta$ 之内就仍按份额记账。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 2 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 4 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 5 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 6 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 7 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 8 | sgpu01/vf3 (10.1.3.1) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 9 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 10 | sgpu03/vf0 (10.1.0.3) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 11 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 12 | sgpu03/vf1 (10.1.1.3) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 13 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 14 | sgpu03/vf2 (10.1.2.3) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 15 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 16 | sgpu03/vf3 (10.1.3.3) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 17 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 18 | sgpu04/vf0 (10.1.0.4) | sgpu02/vf0 (10.1.0.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 19 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 20 | sgpu04/vf1 (10.1.1.4) | sgpu02/vf1 (10.1.1.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 21 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 22 | sgpu04/vf2 (10.1.2.4) | sgpu02/vf2 (10.1.2.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 23 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | RDMA WRITE | 4 QP | 0 | 90 |  |
| 24 | sgpu04/vf3 (10.1.3.4) | sgpu02/vf3 (10.1.3.2) | TCP iperf3 | 4 流 | 0 | 90 |  |
| 25 | sgpu04/vf7 (10.1.7.4) | sgpu02/vf7 (10.1.7.2) | UDP 背景（udp_blast） | — | 30 | 60 | 40 G，HyperFront 不调度也不整形的流量 |

**预期。** 0–30 s 与 V1 相同。30–60 s：四个 VM 各 36.6 G，两类各 18.3 G，24 个流集合应得 6.09 G；RDMA 类在 ECN 标记下退让几个百分点，TCP 类在丢包下退让，都不该低于应得的 $(1-\delta)$ = 85%（低于它账本会把这个流集合判成出借方，份额借给别人）；端口总载荷（租户 + UDP）≈ 190 G，没有流集合低于 3 G。60 s 背景停掉，根回到 184 G，1 s 内回到 V1 的份额。**判据**：通用六条照用、应得值按物理根 146 G 算，判据 1 在 30–60 s 只报数字；这一节的通过条件是"没有流集合低于 3 G"与"两类都不低于应得的 85%"。**图**：`fig/V7_timeline.png`（vf7 的 eth 桶就是背景 UDP）、`fig/V7_executor.png`（拥塞控制之和落到 $R$ 之下的那 30 秒）。

### V8 账本管不到的核心瓶颈（60 s，桶对拥塞控制单独两个臂）

两个接收端 sgpu02、sgpu04 都在 B 侧，两个发送端 sgpu01、sgpu03 都在 A 侧，所有流量都过那根 200 G 的核心线，而两个接收端各自只看得见自己的 200 G 端口，谁都不知道核心存在。0–20 s 每个接收端只有一个 VM 在收（核心上 100 G，不绑定）；20–40 s 每侧再加两个 VM，需求 300 G 撞 200 G 的核心；40 s 退出。表里最后一行 `class=core` 不是流，只告诉 `distill.py` 核心的容量（内层字节口径 184 G），应得值先按每个接收端算、再把过核心的流集合按这个容量重新逐级填平；核心档位由 `split_core_speed.sh` 设、每次记进 `core_speed.txt`。

每个流集合 24 个 QP 让核心口的队列能够积到丢包门限：RoCE 每个 QP 最多约 512 KB 未确认数据在网络里，核心口队列的物理上限就是 QP 总数乘 512 KB，与发送端被允许发多快无关；核心口的出向门限是 35.2 MB，六个流集合 144 个 QP 合起来约 74 MB，是门限的两倍。队列在 ECN 下走不走到门限是拥塞控制自己的事（DCQCN 不会）。

| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | sgpu01/vf0 (10.1.0.1) | sgpu02/vf0 (10.1.0.2) | RDMA WRITE | 24 QP | 0 | 60 |  |
| 2 | sgpu03/vf0 (10.1.0.3) | sgpu04/vf0 (10.1.0.4) | RDMA WRITE | 24 QP | 0 | 60 |  |
| 3 | sgpu01/vf1 (10.1.1.1) | sgpu02/vf1 (10.1.1.2) | RDMA WRITE | 24 QP | 20 | 40 |  |
| 4 | sgpu01/vf2 (10.1.2.1) | sgpu02/vf2 (10.1.2.2) | RDMA WRITE | 24 QP | 20 | 40 |  |
| 5 | sgpu03/vf1 (10.1.1.3) | sgpu04/vf1 (10.1.1.4) | RDMA WRITE | 24 QP | 20 | 40 |  |
| 6 | sgpu03/vf2 (10.1.2.3) | sgpu04/vf2 (10.1.2.4) | RDMA WRITE | 24 QP | 20 | 40 |  |
| 7 | —（交换机内部） | —（A 侧到 B 侧的核心链路 swp21↔swp25） | 核心链路容量 | — | 0 | 60 | 184 G（内层字节口径；链路整形 200G，`split_core_speed.sh`），两个接收端的账本都看不见它 |

设计对这种瓶颈的回答（第 6.4、8.1 节）是：它由两边的拥塞控制在 $R$ 以下处理，流集合的许可速率不因它而变。所以这个场景跑两个臂——桶（缺省）和拥塞控制单独（`HPFT_CC_ONLY=1`）——比的是两者之间。

**预期。** 0–20 s 与 40–60 s 每个 VM 50 G（线上口径），两个臂相同。20–40 s 六个流集合合计接近核心容量，两个臂的合计之差 ≤ 5%，桶臂六个流集合的 Jain 指数不低于拥塞控制单独臂，没有流集合低于 3 G；40 s 瓶颈撤掉后 1 s 内回到 50 G。**判据**：通用六条照用（应得值由 `class=core` 行决定），20 s 与 40 s 两个沿上的判据 1、3 只报数字，两个臂之间的比较是通过条件；另加"核心真的拥塞"：`switch_post.txt` 减 `switch_pre.txt`，`swp21` 的 `ecn-marked-frames` 或 `tx-uc-buffer-discards` 增量必须显著大于零（DCQCN 在 ECN 下通常把核心队列压在丢包门限之下，实测 17% 的帧被标记而零丢弃），否则场景无效。TCP 版（`V8_core_tcp.flows`，20–40 s 加入的四个 VM 跑 TCP）是可选的第二形态。**图**：`fig/V8_timeline.png`、`fig/V8_executor.png`（桶臂），`fig/V8cc_*.png`（拥塞控制单独臂）。

## 五、报告与产物

每次运行的产物：`results/<tag>/`（原始数据，不进 git）、`data/<tag>_*.csv`（`distill.py` 的蒸馏：`vmclass` 100 ms wire、`flowsets` 每阶段每流集合、`goodput`、`events` 每事件收敛、`executor` 与 `executor_summary` 执行面回读、`verdict` 六条判据）、`fig/<场景>_timeline.png` 与 `fig/<场景>_executor.png`（每次运行覆盖，图内标题带 tag 与绘制时刻）、`reports/<tag>.md`（`report.py`：环境、打流表、预期与实测、goodput、收敛、六条判据、机制旁证）。本轮的结果与结论在 `STATUS.md`。

## 六、跑法

### 跑前准备（每一批开始前做一遍）

1. `bash tools/lab-infra/deploy_check.sh --deploy`：四台 DPU 与 host 上的代码和仓库一致，agent 健康，解析器能以登录用户 ssh 到所有对端。改过 PCC 设备码后它会在四台 DPU 上重编，但不重启执行面：随后在每台 DPU 上 `bash /opt/hpft/rp_service.sh start`（`run.sh` 不重启执行面，`quick.sh` 会）。
2. `bash tools/lab_env.sh status` 应显示 environment: HPFT（四台 UPCC=1、doca_pcc 在跑、overlay 在）；不是就 `bash tools/lab_env.sh hpft`。
3. `bash tools/cc_mode.sh sr`：四台 host 的 ROCE_ACCL `selective_repeat_forced_en=1`。所有 HPFT 实验一律跑 SR；寄存器易失，fw reset / Arm 重启后归零；`run.sh` 逐台复核，不满足就中止。查看用 `mlxreg --reg_name ROCE_ACCL --get`，不要看 `lab_env.sh status` 的 `SR current=`。
4. `bash tools/lab-infra/vf_caps.sh sync`：32 个 VF 的 50 G 双端限速。
5. `bash tools/lab-infra/roles.sh all`：四台都同时跑接收端与发送端两套代理（runner 自己也会调它）。
6. 四台 host 每个 VF 的 TCP 执行面恰好一份当前程序：`tc filter show dev dpu1vfN egress` 只有一个 handle，tag 与仓库一致；改过 BPF 后用 `tools/host/edt_reinstall.sh` 重装。
7. 交换机：六个口绑 `hpft_ecn`，出向门限 `alpha_1`，无 egress-scheduler，四个 host 口绑 `motiv-nopfc`，pause 关（`nv show interface swp37s0 qos`）；单交换机形态下四个主机口都在 VLAN 100。
8. 注册表政策是标准政策（每 VM 权重 1、50 G、类 1:1），`e_params` 是这次要测的版本；runner 会把当次 registry 复制进 results/<tag>/。
9. `bash tools/lab-infra/dpu_time_sync.sh apply && sleep 40 && bash tools/lab-infra/dpu_time_sync.sh status`：四台 DPU 的钟对齐到各自 host（偏差应在 ±2 ms 内）。DPU 镜像没有任何时间同步，钟差会让接收端和发送端日志的对比多出几十到几百毫秒的假滞后。
10. 四台 host 的 iperf3 必须带 `--start-at` 补丁（源码 `~/hyperfront/iperf320`）。这个选项不在 `--help` 里，查 `strings /usr/local/lib/libiperf.so.0 | grep start-at`；`run.sh` 开跑前会自己验。
11. V4 之前确认 perftest-enhanced 的 `--rate_limit 10` 在 1 个 QP 上确实压到 10 G（RoCE 上硬件限速一定被拒，要在 stderr 上看到 "providing SW rate limit" 那行才算数）；V6 之前确认三台发送端 BBR 模块可用。

### 一场

```
bash validation/run/run.sh V1_incast V1_bucket_20260908     # 命令行里不要出现 ib_write_bw 字样（runner 的 pkill -f 会误杀）
python3 validation/distill.py V1_bucket_20260908
python3 validation/plot/timeline.py V1_bucket_20260908 V1   # -> fig/V1_timeline.png
python3 validation/plot/executor.py V1_bucket_20260908 V1   # -> fig/V1_executor.png
python3 validation/report.py V1_bucket_20260908             # -> reports/V1_bucket_20260908.md
```

图名前缀缺省是 tag 第一个下划线之前的部分；一个场景的两个臂用第二个参数把图分开。runner 的臂由环境变量选，当次的臂记在 `results/<tag>/arm.txt`：

```
HPFT_RDMA_CC_ALGO=3 bash validation/run/run.sh V1_incast V1swift_<tag>     # RDMA 拥塞控制换 Swift
bash validation/run/run.sh V8_core V8_<tag>                                 # V8 桶臂
HPFT_CC_ONLY=1 bash validation/run/run.sh V8_core V8cc_<tag>                # V8 拥塞控制单独臂（发送端代理停掉、TCP 速率表清空，跑完 roles.sh all 恢复）
HPFT_LAW=1 bash validation/run/run.sh V1_incast V1cap_<tag>                 # 消融：等额封顶 min(c, R/N)
HPFT_LAW=2 bash validation/run/run.sh V1_incast V1eq_<tag>                  # 消融：忽略拥塞控制的等分 R/N
```

V6 的打流表带 `rdma_cc=swift`，runner 自动选 Swift。`HPFT_NO_RP_SAMPLE=1` 关掉发送端执行面的每秒回读（每次查询占用邮箱 13–22 ms）。三遍取平均的图：`timeline.py tagA,tagB,tagC V2`。

### 整套

`run/campaign_20260908.sh <日志>` 依次跑 V1（DCQCN、Swift）、V2、V4、V3、V5、V6，每场跑完蒸馏、出图、写报告；`run/campaign_20260908_b.sh <日志>` 跑 V7，然后把 sn5600 切成双交换机（`tools/lab-infra/switch/split_stage1.sh`、`split_cutover.sh`）、跑 V8 的两个臂、切回单交换机（`split_rollback.sh`）。两个脚本都等 `/tmp/hpft_run.lock` 空出来才开始。

### 复原

runner 在退出时把每个发送端执行面写回缺省（`0xccd 2`、`0xcce 0 22`、`0xcce 0 12`），拥塞控制单独臂结束时重新拉起发送端代理；其余场景不改任何常设配置。V8 之后核一眼 `bash tools/lab-infra/vf_caps.sh status`（四台的八个 meter 都该是 50000000 kbps）和交换机的四个主机口是否回到 VLAN 100。
