# sn5600 一分为二：回环线把一台交换机变成两台

用一根 OSFP 800G 线把 sn5600 的 swp21 和 swp25 接起来，再把主机口分进两个 VLAN，交换机在逻辑上就成了两台：VLAN 101 是"交换机 A"，VLAN 102 是"交换机 B"，两台之间只有这一根线。这根线就是接收端账本管不到的核心链路（设计 v4 §8.1）。

## 端口与主机

| 交换机口 | 主机 / DPU | 侧 | VLAN |
|---|---|---|---|
| swp37s1 | sgpu01 / hpft-dpu | A | 101 |
| swp3s1 | sgpu03 / hpft-dpu3 | A | 101 |
| swp37s0 | sgpu02 / hpft-dpu2（接收端） | B | 102 |
| swp4s1 | sgpu04 / hpft-dpu4 | B | 102 |
| swp21 | 回环线一头 | A | 101 |
| swp25 | 回环线另一头 | B | 102 |

对应关系是 2026-09-04 用 LLDP 和 DPU 口的 MAC 核对的：swp37s0 报 a0:88:c2:bf:46:5f（sgpu02 的 DPU），swp37s1 报 a0:88:c2:bf:9b:21（sgpu01 的 DPU），swp3s1/swp4s1 直接报 hpft-dpu3/hpft-dpu4。现在四个主机口都在 br_default 的 VLAN 100。

为什么不用 29 号口：它拆成了两个 400G（swp29s0/s1），挂着旧的自适应路由、vrf2 和两条静态路由，和 21 号口的拆分状态不一致；21 和 25 都没有任何配置行。

## 为什么要这样配

- **两个 VLAN 而不是一个。** 回环线两头插在同一台交换机上，若两个口在同一个 VLAN，交换机会收到自己的 BPDU、判成环路，RSTP（br_default 现在跑的就是 RSTP）会把一个口置为阻塞。两头各在一个 VLAN，帧从 101 经线进 102，两个 VLAN 靠这一根线连成一个二层域，没有环。
- **回环口关掉 BPDU。** 即使分了 VLAN，BPDU 也不带 VLAN 标签，一头发出、另一头收到，RSTP 仍会认为是环。两个口都 `stp bpdu-filter on`。
- **MTU 9216。** 底层 p1 是 9000，VxLAN 再加约 50 字节。两个口默认已是 9216，脚本里仍显式写一遍。
- **速率先不动。** 线是 800G，插上先按 800G 跑，核心不是瓶颈，先做回归；之后再考虑压到 200G（单口 200G 要看 NVUE 允许的速率档，未验证）。
- **ECN、PFC 先不配。** 回环口默认既不标记也不 PFC，先当纯丢包链路；要看真实的标记型核心瓶颈时再给 swp21/swp25 绑 `motiv_default_ecn`。

## 顺序

1. `split_stage1.sh`：插线之前就能做，只动 swp21/swp25 和 VLAN 定义，主机口不动，四台之间的连通性不变。
2. 插线。`split_verify.sh switch` 看两个口 link up、速率、STP 状态为 forwarding、没有 BPDU 计数。
3. `split_cutover.sh`：把四个主机口分进 101/102。这是唯一有断网的一步，一次 apply，A 侧与 B 侧之间会断几秒。
4. `split_verify.sh hosts`：跨侧 `ping -M do -s 8972`、MAC 表、单对 iperf3、单对 ib_write_bw。
5. 跑一遍 V1 当回归：核心 800G 时数字应与改线前一致。
6. 出问题 `split_rollback.sh`：主机口回 VLAN 100、回环口 down。

配置备份在 `sn5600_config_20260904.cmds`（改线前的完整命令式配置）。

## 让核心成为瓶颈：出口整形，不拆口

以太网没有 200G 到 400G 之间的链路速率（只有 100/200/400/800G 这些档，拆口也只能落在这些档上）。要一个任意的核心速率，用交换机的出口整形：`split_core_shape.sh 300` 把 swp21（A→B 出口）和 swp25（B→A 出口）都限到 300G，`split_core_shape.sh off` 撤掉，`status` 看现状。链路仍是 800G，不拆口、不重载 switchd、主机口不闪断，数值随时改。整形器是真实的队列，给这两个口再绑 ECN 配置（`nv set interface swp21,swp25 qos congestion-control profile motiv_default_ecn`）就是标记型的核心拥塞。每次验证运行把当时的整形值记在 `results/<tag>/core_shaper.txt`。

哪个值绑得住：核心上只有 A 侧到 B 侧的流量。单接收端（只有 sgpu02 收）时 A→B 最多 200G，被接收端口先限住，核心要低于 200G 才绑；B 侧两台都收时 A→B 的需求是 400G，核心在 200–400G 之间就绑，而单接收端的流量不受影响——这是 300G 这类值的用法，但需要验证套件支持第二个接收端。

## 现状（2026-09-04）与标定

核心整形到 **300G**（`core_shape`，两个方向），swp21/swp25/swp37s1 与其余主机口一样绑了 `motiv_default_ecn` 与 `motiv-nopfc`：全部端口都支持 ECN，PFC 全关。

整形值用平滑的 RDMA 标定过（sgpu01→sgpu02，每对 4 QP，应用口径；线速 = 应用口径 × 1.073（1024 字节报文）× 1.05（VxLAN 封装）：设 30G 时单对 26.3 G ≈ 29.6 G 线速；设 150G 时四对 131.4 G ≈ 148 G 线速；设 300G 时四对 166.8 G，就是 HyperFront 按 184 G 根容量分出来的量，核心不再限。单位是 kbps、语义是出口线速，确认无误。

**用 TCP 打满核心的测试不能用来标定整形值。** 八对不受围栏管的 TCP（A 侧两台各四对打 B 侧两台，每对 4 流，需求约 370 G）合计只有约 110 G，每对每 10 秒重传几千次；整形关掉（核心 800G）结果一模一样，核心口的缓冲、队列、WRED 丢弃全为零，发送端 DPU 的 OVS 上送丢包也没有增加。原因在接收端 VF 的 50 G 丢包型 meter：单独一对 TCP 能跑到 46.9 G 但 8 秒里重传 4 千到 2.5 万次，就是 TCP 撞 meter 的常态；同一台主机同时起两对每对只剩 33 G、四对只剩 16 到 19 G，是四对同时在 meter 上丢包、协议栈忙于恢复的结果。HyperFront 管着的 TCP 被围栏压在 VM 上限之下，不会撞 meter（V2 里 sgpu01 四对 TCP 各 23 G、合计 92 G 没有问题），所以这是"不受管的 TCP 满发撞 meter"的测试方法问题，不是核心的问题。标定核心整形一律用 RDMA。
