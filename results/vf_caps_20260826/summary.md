# 每 VF 50G 双端限速的验证（2026-08-26）

**结论**：四台机各 8 个 VF、每个 VF 50G，两层限速都成立。发送端 devlink 把一个 VF 的上行钉在 48.8G（RDMA）/ 49.4G（TCP）；接收端 OVS meter 把一个 VF 的下行钉在 44–50G，而且在三台发送端各自守着 50G、合计 150G 打同一个 VF 的时候，只有 meter 能把它压回 50G（RDMA 46.9G、TCP 50.5G）。两层都关时同一个 VF 能跑到 183–197G，说明限的是配置而不是链路。

## lab 形态

| 项 | 取值 |
|---|---|
| 机器 | sgpu01/02/03/04，各一块 BF3，p1 200G，常驻 VxLAN overlay 星型（中心 sgpu02） |
| VF | 每台 8 个（`dpu1vf0–7`，IP `10.1.<vf>.<机器号>`），`tools/lab-infra/vf_setup.sh` 从 registry 取数量、从 sysfs `virtfn` 取 PCI 地址；`registry_refresh.py` 把内核决定的 rdma 设备名 / MAC / PCI 回填两个 registry |
| 上行限速 | 每个 VF 的 representor 上 `devlink port function rate tx_max = 50G`，在 VF 自己的 DPU 上（`hw_maxrate.py --sync`，policy `max_rate_bps`） |
| 下行限速 | 每个 VF 自己的 DPU 上一条 OVS meter（`meter=11+vf, kbps, drop band 50G`）+ 流表 `priority=120,ip,nw_dst=<VF IP> → meter,NORMAL`；只丢不标 |
| 一键 | `tools/lab-infra/vf_caps.sh sync|clear|status`（四台 DPU 全部）；`lab_env.sh meter on|off|status` 是它的别名 |
| 本验证的环境 | UPCC=1、四台 agent 停、发送端 PCC 执行面在跑（它对不认识的流按每 QP 5G 兜底，所以 RDMA 用 16 QP 把这个天花板放到 80G）；重传 GBN；TCP cubic 无 ECN；交换机 `swp37s0` 绑 `motiv_default_ecn`，无 TC 隔离 |

## 打流表（每点 20 s；接收端全部是 sgpu02/vf4 = 10.1.4.2，直连对）

| 行 | src | dst | 类型 | 规模 | 用于哪些点 |
|---|---|---|---|---|---|
| 1 | sgpu01/vf4 (10.1.4.1) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 16 QP | 单对四臂 + 两个 incast 点 |
| 2 | sgpu01/vf4 (10.1.4.1) | sgpu02/vf4 (10.1.4.2) | TCP iperf3 | 16 流 | 单对四臂 + 两个 incast 点 |
| 3 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 16 QP | 两个 incast 点 |
| 4 | sgpu03/vf4 (10.1.4.3) | sgpu02/vf4 (10.1.4.2) | TCP iperf3 | 16 流 | 两个 incast 点 |
| 5 | sgpu04/vf4 (10.1.4.4) | sgpu02/vf4 (10.1.4.2) | RDMA WRITE | 16 QP | 两个 incast 点 |
| 6 | sgpu04/vf4 (10.1.4.4) | sgpu02/vf4 (10.1.4.2) | TCP iperf3 | 16 流 | 两个 incast 点 |

RDMA 和 TCP 分开跑，不混。

## 四臂（单对，行 1 或行 2）

| 臂 | 发送端 devlink | 接收端 meter | 应用 goodput RDMA / TCP | 接收端 p1 线上（meter 之前）RDMA / TCP | 送达 VF（meter 之后）RDMA / TCP |
|---|---|---|---|---|---|
| none | 关 | 关 | 193.0 / 196.4 | 185.1 / 198.8 | 182.7 / 197.5 |
| devlink | 50G | 关 | 48.8 / 49.4 | 46.7 / 50.0 | 46.1 / 49.7 |
| meter | 关 | 50G | 中途报错（见下）/ 50.0 | 116.5 / 53.8 | 44.4 / 50.3 |
| both | 50G | 50G | 48.8 / 49.4 | 46.5 / 50.0 | 45.9 / 49.7 |

（单位 Gb/s；"线上"含 VxLAN 外层头，"送达 VF"是 VF 自己的接收计数：RoCE 走 IB 口计数、TCP 走网卡计数。）

## incast（三台发送端同打 sgpu02/vf4，行 1+3+5 或行 2+4+6）

| 臂 | 应用 goodput（sgpu01 / 03 / 04） | 线上合计 | 送达 VF |
|---|---|---|---|
| devlink 开、meter 关，RDMA | 48.8 / 49.0 / 48.9 | 138.8 | **137.0**（发送端各守 50G，VF 收到 3 倍） |
| devlink 开、meter 关，TCP | 49.4 / 49.4 / 49.4 | 147.8 | **146.9** |
| 双开，RDMA | 报错 / 15.7 / 15.8 | 92.4 | **46.9** |
| 双开，TCP | 24.6 / 0.8 / 25.8 | 55.0 | **50.5** |

## 判读

1. **发送端 devlink 成立、精度好**：单 VF 上限 50G，RDMA 48.8G、TCP 49.4G（含 VxLAN 头后线上正好 50.0G，说明 devlink 计的是外层字节）。三台各自开 devlink 时每台都守在 49G。
2. **接收端 meter 成立、是唯一能管住 incast 的一层**：三台合计 150G 打一个 VF，meter 之后送达 46.9G（RDMA）/ 50.5G（TCP），超发 ≤ 1%。OVS 硬件卸载的 meter 不报丢包字节（band 计数恒 0），所以丢包量用"线上 − 送达"看：TCP 单对 53.8 → 50.3，RDMA 单对 116.5 → 44.4。
3. **纯丢包策略器对 RDMA 的副作用如预期**：meter 单独开时 16 QP 的 RDMA 以 116G 冲进 50G 的策略器，发送端 GBN 重传撑不住，perftest 报 "retry counter exceeded"（QP 被打死）；双开的 incast 里 sgpu01 那一路同样被打死，另外两路各 15.7G。这不是限速失效，是 motivation 1-3 要讲的现象（只丢不标，DCQCN 收不到信号），本验证里只把它记为事实。
4. **TCP incast 在策略器下三路极不均**：24.6 / 0.8 / 25.8——三台的 Cubic 在同一个丢包策略器下自己也分不匀，同样是 1-3 的素材。
5. 信心：高。四臂 + 两个 incast 点数字自洽，控制臂 183–197G 证明没有别的瓶颈。

## 产物

`run.sh`（复现）；`results/<点>/` 下每点：perftest / iperf3 原始输出、meter 前后快照、devlink 读回、p1 线上计数、VF 接收计数、t0/t1；`results/run.log` 是一行摘要。
