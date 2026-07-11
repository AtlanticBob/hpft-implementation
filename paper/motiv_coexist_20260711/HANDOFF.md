# 交接：RDMA/TCP 共存不公平的动机实验（2026-07-11）

这份文档是给重做这个动机实验的新 agent 的单一入口。实验的目的是用一张
图证明：当 lossy RDMA 和 TCP 共享交换机的一个瓶颈队列时，公平性会系统
性地崩坏，而"给两类流量分开排队"这种朴素的 QoS 方案救不全。这张图放在
论文的 §2.2（Background 里讲 RDMA/TCP 共存问题）那一节，为后文"我们需要
一个既隔离租户、又隔离协议的方案"做铺垫。

之前已经完整做过一轮，数据、图、汇总都在本目录里（下面列出）。之所以
可能要重做，是因为想改实验设置或图的呈现。这份文档把做法和踩过的坑都
讲清楚，让重做时不用从头摸索。先读本目录的 `summary.md`（上一轮的结论
和数据表），再读本文件了解怎么复现和有哪些坑。

## 这张图讲什么

图是一个 2×2 的主图加一张补充图。横轴是 RDMA 流数和 TCP 流数的比例，
从 4:1 扫到 1:4 共七个点。纵轴是每一条流分到的 goodput。四个子图分两个
维度对比：一个维度是队列配置（共享队列开 ECN、还是按类分队列），另一个
维度是 RDMA 的丢包恢复方式（GBN 还是 SR，即 selective repeat）。补充图
单独画"共享队列不开 ECN"这个最惨的场景（纵轴范围小，因为总量只有 1G 出头）。

图要讲三件事。第一，共享队列没有公平的配置点：不开 ECN 是两类同归于尽，
开了 RoCE 惯用的 ECN 又轮到 TCP 被压到三分之一。第二，按类分队列确实能
把两类隔开——但这正好引出后文对 TC 方案的批评：TC 是静态的、队列数量
有限，而云环境需要租户和协议的双重隔离，TC 只能保一头。第三，SR 是
必要的（GBN 连自己队列里多条流都撑不住，会重传风暴崩溃），但不充分
（共享队列的不公平在 SR 下原样存在），这样堵住"上 SR 就够了"的评审退路。

每个柱子的画法（上一轮定的样式）：一根柱子代表一个比例点，自底向上先
堆 TCP 各流（橙色），再堆 RDMA 各流（蓝色），同类里的每条流之间用细的
黑色全包围边框分开，每段的长度就是那条流的带宽。绘图脚本在本目录，
可以直接改样式。

## 实验拓扑和瓶颈

发送端是 sgpu01 的 dpu1，接到交换机的 swp37s1 端口；接收端是 sgpu02 的
dpu1，接到 swp37s0 端口。**注意端口映射：swp37s0 是接收端（sgpu02）
那一侧，也就是瓶颈出口；swp37s1 是发送端侧。** 这一点上一轮踩过坑——
最初以为是反的，后来用"降速时哪个口的 25G 生效、哪个口的出向帧数在涨"
实测纠正过来。交换机是 sn5600，跑 Cumulus Linux 5.11，是一台共享的
交换机（有 28 个口在用），所以改配置只能动我们这两个口，别动全局。

瓶颈是这么造出来的：把接收端的 p1 链路用 ethtool 降到 25G，发送端保持
200G，这样拥塞一定发生在交换机朝接收端的那个出口队列（swp37s0 的
egress），而这正是 ECN 和队列调度生效的地方。每个比例点下，单条流的
公平份额在 5 到 12.5G 之间，动态范围足够看出差别。

做这个实验期间，实验室的方案 E 那套东西（rx/tx agent、PCC RP）要全部
停掉，让 RDMA 回到网卡固件原生的 DCQCN、TCP 不被我们限速。两端 DPU 的
p1 还要关掉 PFC 和全局 pause，让端点是彻底 lossy 的。这些在下面的
"跑之前的准备"里有命令。

## 三种队列配置

三种配置都是在交换机上通过 NVUE profile 切换的。上一轮建的三个 profile
现在还在交换机上、没有绑定，可以直接复用：`motiv-noecn`（把 TC0 的 ECN
关掉）、`motiv-split`（TC3 和 TC0 按 60/40 加权轮询）、`motiv-nopfc`
（把 PFC 关掉，保持 lossy）。

**noecn，共享队列不开 ECN。** TCP 和 RDMA 都用默认的 DSCP 0，挤在同一个
TC0 队列，队列退化成纯 tail-drop。给 swp37s0 绑上 `motiv-noecn` 这个
congestion-control profile。结果是同归于尽：DCQCN 收不到任何拥塞信号
就线速盲发，把队列打爆，GBN 下每轮出现几万次乱序（整窗重传风暴，多流时
QP 直接死），25G 的链路总共只跑出 1G 出头。

**ecn-shared，共享队列开 RoCE 式 ECN。** 还是同一个 TC0 队列，但用交换机
原本就有的默认 ECN 配置（超过 156KB 就标记，概率 100%），不绑任何自定义
profile（把 noecn 解绑回默认即可）。Spectrum 的行为是：超过阈值后，带
ECT 标记的包（RoCE 数据包会自动带）被 ECN 标记，不带 ECT 的包（TCP，
因为没启用 ECN）被按同一条曲线直接丢弃。于是 DCQCN 温和地贴着阈值跑、
TCP 一冒头就挨丢，RDMA 每流拿到 TCP 的大约三倍。

**split，按类分队列加 60/40 加权轮询。** RDMA 流打上 `--tclass=106`
（等于 DSCP 26 加 ECT），进 TC3 队列；TCP 保持默认 DSCP 0，留在 TC0；
两个队列按 TC3:TC0 = 60:40 做 DWRR。给 swp37s0 绑 `motiv-split` 的
egress-scheduler，同时给我们两个口绑 `motiv-nopfc`（因为 SP3 在这台
交换机上默认是无损的，要关掉 PFC 才能保持 lossy 语义）。类是不是真的
分开了，用交换机每个 TC 的出向帧数差分来验证（RDMA 的帧应该只在 TC3
涨，TCP 只在 TC0 涨）。

## GBN 和 SR 怎么切

RDMA 的丢包恢复方式由两端 BF3 的固件开关 `RDMA_SELECTIVE_REPEAT_EN`
决定，False 是 GBN、True 是 SR。切换要在两端都
`sudo mlxconfig -y -d 03:00.0 set RDMA_SELECTIVE_REPEAT_EN=1`（或 0），
然后两端 DPU 都 reboot 才生效。这意味着一轮实验里只能切一次方向：先把
GBN 的全部点（三种配置乘七个比例）跑完，再切 SR、跑完，最后切回 GBN。
不能来回横跳。当前两端是 GBN。

**每次 DPU reboot 之后必须重做几件事**（reboot 会把 host 的 VF 掀掉）：
在两台 host 上跑 `bash ~/vf_setup.sh` 重建 4 个 VF，然后把 VF 的 MTU
钉回 1500（`sudo ip link set dpu1vfN mtu 1500`）——因为 GBN 那轮是在
MTU 1500 下跑的，SR 那轮要保持一致才可比。还要重新把接收端 p1 降到
实验用的速率、关掉两端 p1 的 PFC 和 pause。

## 测量方法

一个数据点就是：R 条 RDMA 流（ib_write_bw）加 T 条 TCP 流（iperf3）同时
打向同一个目的地（接收端 vf0），跑 60 秒，读每条流的自报 goodput。同时
从交换机取每个队列的差分计数（发送帧数、ECN 标记数、丢弃数）作为机制
证据，从发送端读 RC 乱序计数器（`packet_seq_err`）差分作为重传证据。
本目录的 `motiv_run.sh` 就是跑一个点的脚本，用法是
`motiv_run.sh <标签> <RDMA流数> <TCP流数> <tclass>`，共享配置传 tclass=0，
split 配置的 RDMA 传 tclass=106。一轮完整的矩阵是 42 个点，加上两次
reboot，大约两个小时。

比例点上一轮用的是 4:1、3:1、2:1、1:1、1:2、1:3、1:4，每种配置七个点。

## 踩过的坑（重做时照着避）

**pkill 会误伤自己。** `pkill -f <模式>` 是按整条命令行做正则匹配的。
如果 pkill 和它要杀的目标程序写在同一条 ssh 远程命令里，模式会匹配到
承载它自己的那个 shell 的命令行（里面就有目标程序的完整调用文本），
于是把自己杀了。上一轮这个坑咬了两次，一次让浸泡流量连续 9 轮静默失败。
规则是 pkill 和目标程序的启动永远分成两条 ssh，或者用 `[w]` 这种字符
类技巧（`ib_write_b[w]`）让模式不匹配自己那行。

**perftest 有三个坑。** 一，duration 模式的 `-D` 参数两端必须一致，
不一致会报 "Failed to negotiate parameters"，分阶段跑时要为每个阶段起
匹配的 server。二，结果表里的 iterations 列在 -D 模式下不可靠（大约是
真实消息数的一半），算总量要用 BW average 乘时长。三，`--rate_limit`
是硬件档位制，只接受 2.5/5/10 这样的离散值，传 2 会直接报错退出——
这个失败很安静，容易以为流量在跑其实没跑。

**Cumulus 的 qos 计数器是慢轮询的。** 取队列计数的前后快照做差分，两次
快照之间要等大约 25 秒，否则读到的还是旧值。`motiv_run.sh` 里已经有这个
sleep 25。

**NVUE 建 profile 必须绑到某个口才能 apply。** 上一轮试过建一个不绑定的
profile 直接 apply，会报 "'motiv-split' is not one of [...]"。所以建
profile 和绑定要一起做、一起 apply。

**systemd-run 的临时单元停了之后偶发 "Unit already exists"。** 需要先
`systemctl reset-failed <unit>` 再重新拉起。

**matplotlib 和 numpy 2.x 不兼容。** 上一轮画图时系统的 matplotlib 撞
numpy 2.x 报 `_ARRAY_API not found`，`pip install --user -U matplotlib`
升级到 3.10 就好了。

**registry 政策会漂移。** 实验期间往 DPU 上直接 scp 或 ssh 改 registry，
容易和 repo 里的版本对不上。repo 的 `config/lab-registry.json` 是唯一
真值，实验完要把 DPU 上的副本和 repo 对齐。

## 图怎么生成

上一轮把 42 个点的逐流数据汇总成 `motiv_perflow.csv`（156 行，每行一条
流的 goodput），再用一个 matplotlib 脚本画堆叠柱状图。配色用 Okabe-Ito
的蓝橙对（对色盲友好），TCP 橙、RDMA 蓝，每条流之间细黑边框。样式上一轮
改过几版，最后定的是：不画灰色的"未用带宽"、不在线速处画虚线、每段用
细黑色全包围边框。PDF 是矢量版可以直接进论文。

## 上一轮的产物（本目录）

`summary.md` 是结论和 42 组数据表。`motiv_perflow.csv` 是逐流数据。
`motiv_fig.png/pdf` 是 2×2 主图，`motiv_noecn_fig.png/pdf` 是无 ECN
补充图。`motiv_run.sh` 是跑一个点的脚本。`switch_config_backup.txt` 是
实验前交换机的全量配置备份。各个 `gbn_*` / `sr_*` 子目录是每个点的原始
perftest/iperf3 输出、交换机队列快照、乱序计数。

## 跑之前的准备和跑完的恢复

准备：把 soak 流量的 cron 注释掉（`crontab -e`，给 soak_traffic 那行
加 #），停掉方案 E 的三个服务（两端的 hpft-rxagent-e、hpft-txagent-e，
sgpu01 的 hpft-pace-shim）和 PCC RP，把 EDT 的 pace 抬到线速或直接不管，
两端 p1 关 PFC 和 pause，接收端 p1 降到 25G。

恢复：把交换机上绑的 profile 全部解绑（回默认），两端 DPU 切回 GBN
（如果动过），接收端 p1 恢复 200G，重建 VF（reboot 过的话），重启方案 E
的三件套和 RP，恢复 soak 的 cron。上一轮的恢复清单在自动 memory
`eurosys-rd-fairness-design` 里也有一份。交换机的三个 motiv profile
可以留着不删，下次直接复用。
