# Motivation 1.2 执行手册（异构 CC 在 incast 下的竞争）

这份手册让后续实验者能**复现或修改**1.2 的三个子实验。读完能回答：拓扑
和瓶颈怎么搭、GBN/SR/DCQCN 怎么切、交换机队列与 ECN 怎么配、每个点怎么
跑、数据怎么解析成图。机制性的"为什么"见各图旁的结论与 `_archive/`。

> **一句话前提**：所有实验都是 16 条 RDMA 流 + 16 条 TCP 流（4 对 VF ×
> 各 4+4）同时打向 100G 瓶颈，RDMA=固件原生 DCQCN 的 lossy RoCEv2，
> TCP=Cubic 无 ECN，两类共享同一个交换机出口队列。变量只有三样：
> **重传算法（GBN/SR）、交换机 ECN 标记带、CC 恢复激进度**。

---

## 0. 目录地图

```
exp1_ecn_band/      1.2.1 ECN 标记带阶梯（固定队列深度）  ← 主图
    runs/           30 个运行：band<Kmin>-<Kmax>_<arm>_run<N>
    runs.csv perflow.csv   ← tools/parse.py 生成
    plot.py fig_ecn_band.{png,pdf}
exp2_timeseries/    1.2.2 带宽随时间（无独立运行，复用 exp1 的 band96-143）
    plot.py fig_timeseries.{png,pdf}
exp3_cc_scan/       1.2.3 CC 恢复激进度扫描（工作点固定在 band71-120）
    runs/           30 个运行：<config>_<arm>_run<N>
    runs.csv perflow.csv plot.py fig_cc_scan.{png,pdf}
tools/              全部可执行脚本（见 §5）
_archive/           过时/失效/诊断用数据 + 历史文档（见 §6）
switch_config_backup.txt   实验前交换机全量配置备份
```

运行目录命名规范：`<config>_<arm>_run<N>`，`arm ∈ {gbn, sr}`，`N` 从 1。
`tools/parse.py` 按这个正则聚合，改名会破坏解析。

---

## 1. 实验平台与固定拓扑

| 项 | 值 |
|---|---|
| 发送端 | sgpu01，4 个 VF `dpu1vf0..3` = 10.1.{0..3}.1，RDMA 设备 mlx5_6..9 |
| 接收端 | sgpu02，VF 同构 = 10.1.{0..3}.2，mlx5_6..9；host PF netdev=`dpu1`(38:00.1) |
| 交换机 | sn5600（Cumulus，共享，28 口在用）；瓶颈出口 = **swp37s0**（接 sgpu02 侧），swp37s1 接 sgpu01 侧 |
| DPU | `ssh hpft-dpu`(发送侧)、`ssh hpft-dpu2`(接收侧，经 sgpu02 跳板) |
| perftest | 两端 `~/hyperfront/perftest-26015/ib_write_bw`（系统版会 crash） |

**瓶颈制造**：接收端 dpu2 的 p1 用 ethtool 降到 100G，发送侧保持 200G →
拥塞固定在 swp37s0 egress。两端 p1 关 PFC + 全局 pause（lossy 前提）。

**流量口径（见 `tools/parse.py` 头注）**：
- goodput = 应用自报（perftest BW-average 列 / iperf3 `sum_received`），纯
  payload，重传不重复计。**这是柱状图的主口径。**
- wire = 接收端硬件 vport 计数（vport-meter，含头部 + GBN 的乱序丢弃到达）。
  `wire/goodput − 1` = 线上税，GBN 的重传垃圾在此显形。仅 vpm 采样接入后
  的运行（exp1 的 r2/r3、exp3 全部）有此列。

---

## 2. 三个正交旋钮

### 2.1 GBN ↔ SR（ROCE_ACCL 寄存器，秒级，DCQCN 兼容）

```bash
bash hpft-v2/tools/cc_mode.sh sr     # 两端 selective_repeat_forced_en=1
bash hpft-v2/tools/cc_mode.sh gbn    # =0
bash hpft-v2/tools/cc_mode.sh status # 核对
```
只对**新建 QP** 生效（切换后重启流量即可），不动 CC，寄存器易失（fw reset
后归零）。**这是本 lab 唯一正确的 SR 开启法**，详见 `docs/cc_mode_switching.md`。

### 2.2 DCQCN 恢复参数（发送端 host PF sysfs，运行时生效）

`tools/motiv12_env.sh rdma slow|default|fast` 写
`/sys/class/net/dpu1/ecn/roce_rp/{rpg_time_reset,rpg_ai_rate,rpg_hai_rate}`：

| 档 | time_reset | ai_rate | hai_rate |
|---|---|---|---|
| slow | 1200 | 1 | 10 |
| default | 300 | 5 | 50 |
| fast | 75 | 50 | 500 |

VF 的 DCQCN 参数**只认 host PF 的 sysfs**（DPU Arm 侧/debugfs 全部无效，
已逐一排除）。

### 2.3 TCP Cubic 退避（发送端内核模块参数）

`tools/motiv12_env.sh tcp slow|default|fast` 写
`/sys/module/tcp_cubic/parameters/beta`：slow=410 / default=717 / fast=922。
（`bic_scale` 只读，增长侧不可调；退避侧 beta 可调。）

**注意**：TCP beta 只在 TCP 真丢包时才进场。原 1.2（深缓冲、零丢包）里它
完全无效；1.2.3 在贴顶工作点上它才成为强旋钮。

---

## 3. 交换机配置：队列深度与 ECN 标记带

**共享交换机安全隔离**：把我们两口的流量导入全网无人使用的 **TC5**，队列
深度（buffer alpha）与 ECN 阈值都配在 TC5 上，绝不碰默认 TC0（波及 28 口）。
TCP 与 RDMA 仍在**同一个** TC5 队列（不拆分，符合"同一 TC"前提）。

### 3.1 上 TC5 隔离方案（每次实验前）

```bash
ssh sn5600 'nv set qos egress-queue-mapping default-global switch-priority 5 traffic-class 5
nv set qos advance-buffer-config default-global egress-lossy-buffer traffic-class 5 shared-alpha alpha_1_4
nv set qos mapping motiv12tc5 trust port
nv set qos mapping motiv12tc5 port-default-sp 5
nv set interface swp37s0 qos mapping profile motiv12tc5
nv set interface swp37s1 qos mapping profile motiv12tc5
nv set interface swp37s0 qos congestion-control profile <ECN_PROFILE>
nv config apply -y'
```

- `alpha_1_4` → 单队列上限 **实测 42.8MB**（`alpha` 语义是动态的，池内空闲
  越少上限越小；空池下 1/32,1/16,1/8,1/4 ≈ 2.1/4.1/7.7/13.9MB 是**标称**，
  实际以跑动中直读为准，见 §3.3）。1.2.1/1.2.3 固定用 `alpha_1_4`。
- `<ECN_PROFILE>` 见 §3.2。

### 3.2 ECN 标记带 profile（1.2.1 的五档 + 1.2.3 的工作点）

阈值 profile 一次创建即持久（当前交换机上已存在 motiv12_m2..m5、b16 等）。
重建命令模板（TC5，Pmax 全 20%）：

```bash
ssh sn5600 'nv set qos congestion-control <NAME> traffic-class 5 ecn enable
nv set qos congestion-control <NAME> traffic-class 5 red disable
nv set qos congestion-control <NAME> traffic-class 5 min-threshold <Kmin_bytes>
nv set qos congestion-control <NAME> traffic-class 5 max-threshold <Kmax_bytes>
nv set qos congestion-control <NAME> traffic-class 5 probability 20
nv config apply -y'
```

| exp1 档位（相对 C=42.8MB） | profile | Kmin/Kmax 配置值 | 实落(×0.9546 取整) |
|---|---|---|---|
| band03-12  | motiv12_b16 | 1.4M / 5.6M | 1.34M / 5.35M |
| band25-47  | motiv12_m2  | 11M / 21M   | 10.5M / 20.1M |
| band47-96  | motiv12_m3  | 21M / 43M   | 20.0M / 41.1M |
| band71-120 | motiv12_m4  | 32M / 54M   | 30.5M / 51.5M |
| band96-143 | motiv12_m5  | 43M / 64M   | 41.1M / 61.1M |

**关键设计**：混跑时 TCP 把队列养到 ~40MB（rmem 均衡点）。Kmax 不越过这个
运行点，RDMA 全程被标记 → 饿死。所以标记带必须"贴队列上限"配。**exp1 的
横轴就是这个标记带位置（归一化到 C）**，是全部 1.2 的核心自变量。

> ⚠ **阈值 cell 取整系数实测 ×0.9546**（配 16M 实落 15.27M、配 48M 实落
> 45.79M）。band 名字用的是**实落值**的百分比。band96-143 的 Kmin 实落
> 41.05M < C=42.8M，所以那一档仍有零星标记（图上 CNP 非零来源于此，
> 不是矛盾）。

### 3.3 直读真实队列上限（负载下）

alpha 是动态语义，务必在负载下核对实际上限：
```bash
# 加载后，跑动中每 3s 读一次
ssh sn5600 'nv show interface swp37s0 qos buffer egress-traffic-class' | grep '^ *5'
# 输出列：TC pool mode reserved Current-Usage Max-Usage alpha
# Max-Usage 就是实测上限（1.2.1 期间稳定钉在 42.77MB）
```
`tools/run_point.sh` 已自动在每个点跑动中采 3 次到 `buf_samples.txt`。

### 3.4 复原交换机（★每次实验结束必做★）

```bash
ssh sn5600 'nv unset interface swp37s0 qos mapping
nv unset interface swp37s1 qos mapping
nv set interface swp37s0 qos congestion-control profile ecn_incast_bzx
nv unset qos egress-queue-mapping default-global switch-priority 5
nv unset qos advance-buffer-config default-global egress-lossy-buffer traffic-class 5
nv config apply -y'
# 核对：SP5→TC0 回默认、profile=ecn_incast_bzx、swp37s0 流量回 TC0
```
profile 定义本身（motiv12_*）留着复用，只解绑不删。

---

## 4. 跑一个点 & 跑一轮

### 4.1 单点

```bash
cd hpft-v2/paper/motiv_hetero_cc_20260714
OUTROOT=exp1_ecn_band/runs bash tools/run_point.sh band71-120_gbn_run1
```
`run_point.sh` 做的事：清场 → 起 16 iperf3 + 16 perftest server → 抓前置
计数器（交换机 qos / 端侧 seq_err+cnp / 接收端 marked）+ 启动 dpu2 vpm
采样 → 32 client 齐发 60s → 抓后置计数器 + 跑动中 ping/buffer → 拉回
vpm_series.csv。`NTCP=0` 则只跑 16 RDMA（诊断用）。

### 4.2 一轮扫描（以 exp1 为例）

前提：§3.1 上好 TC5 + alpha_1_4。然后按档位循环，**同一档内先跑完一个
arm 再切另一个**（减少寄存器切换）：

```bash
cd hpft-v2/paper/motiv_hetero_cc_20260714
OUT=exp1_ecn_band/runs
for band_profile in "band03-12:motiv12_b16" "band25-47:motiv12_m2" \
     "band47-96:motiv12_m3" "band71-120:motiv12_m4" "band96-143:motiv12_m5"; do
  band=${band_profile%%:*}; prof=${band_profile##*:}
  ssh sn5600 "nv set interface swp37s0 qos congestion-control profile $prof; nv config apply -y"
  bash ../../tools/cc_mode.sh gbn
  for n in 1 2 3; do OUTROOT=$OUT bash tools/run_point.sh ${band}_gbn_run${n}; done
  bash ../../tools/cc_mode.sh sr
  for n in 1 2 3; do OUTROOT=$OUT bash tools/run_point.sh ${band}_sr_run${n}; done
done
bash ../../tools/cc_mode.sh gbn   # 回默认
# 然后 §3.4 复原交换机
```

exp3 同理，但**固定 band71-120（motiv12_m4）**，循环维度换成 CC 配置：
每个 config 跑前用 `tools/motiv12_env.sh rdma|tcp <档>` 设旋钮，跑后用
`... default` 复位。config 名 = `{rdma-slow,default,rdma-fast,tcp-slow,tcp-fast}`。

> ⏱ 前台单命令 10 分钟上限；一轮 30 点需分批或后台跑。每点约 90s
> （60s 流量 + 25s 交换机慢轮询等待 + 采样拉取）。

### 4.3 平台注意（会咬人的坑）

- **pkill 自匹配**：pkill 与目标进程的启动**分两条 ssh**，或用 `ib_write_b[w]`
  字符类。run_point.sh 已守规。
- **iperf3/perftest server 先起后连**，pgrep 判活的模式别含字面端口号
  （会匹配到承载它的 shell）。
- **变速类操作后**用 `ping -c 100 -i 0.05` 验 fabric 干净（反复变速会诱发
  10-200ms RTT 尖峰病，约 1h 自愈）。
- **DPU reboot / fw reset 后**跑 `tools/post_reboot_recover.sh`（重建 VF +
  GUID + MTU1500 + 100G 瓶颈 + pause/PFC 关）。**VF node GUID 必须非零**，
  否则 vpm-meter 之外一切照常但 rdma_cm 类工具会崩（已固化进 vf_setup.sh）。

---

## 5. 数据 → 图（全部从 CSV 出，无硬编码）

```bash
cd hpft-v2/paper/motiv_hetero_cc_20260714
python3 tools/parse.py exp1_ecn_band     # → runs.csv + perflow.csv
python3 tools/parse.py exp3_cc_scan
python3 exp1_ecn_band/plot.py            # → fig_ecn_band.{png,pdf}
python3 exp2_timeseries/plot.py          # 复用 exp1 的 band96-143 run3 时间序列
python3 exp3_cc_scan/plot.py
```

`runs.csv` 列：config, arm, run, rdma_gbps, tcp_gbps, total_gbps,
wire_ib_gbps, wire_eth_gbps, rtt_ms, seq_err, cnp, ecn_marked,
tcp_retrans, dead_rdma_flows。`plot.py` 内对每格取运行均值。

**异常剔除记录**（写在 plot.py 的 EXCLUDE 里，非静默）：
- exp1 `band47-96_gbn_run1`：重传风暴离群点（1.4 万 NAK、5000 万 ECN 标记，
  约为其它运行 70×），从均值剔除。
- exp3 无剔除；但 band71-120 工作点份额漂移大（单格 min-max 可达 0.7-31G），
  图仅作定性，精确份额需更长窗口。

`tools/` 脚本清单：`run_point.sh`(跑点) `motiv12_env.sh`(ECN/RP/beta 旋钮)
`vpm_sample.py`(接收端硬件采样，部署在 dpu2:/tmp) `parse.py`(解析)
`post_reboot_recover.sh`(reboot 后恢复)。

---

## 6. `_archive/` 里有什么

只保留三份历史文档（机制"为什么"的出处，正文引用它们）：
- **summary.md** — 全程叙事：从最初深缓冲饿死、到共享缓冲温和阶梯、到
  固定深度贴顶的完整推理链，含每步的机制证据（CNP 对账、线上税推导等）。
- **run_log.md** — 逐轮时间线与环境定案。
- **fixed_depth_table.md** — 固定深度实验的逐点数据总表。

走过的弯路（原始运行数据）已删除，其结论已浓缩进上述文档与各 plot.py 的
EXCLUDE 注释。曾经的五类弯路备忘（避免重蹈）：
1. **默认 61MB 深缓冲**：TCP 白嫖零丢包把 RDMA 全标记饿死在 0.3G，参数只在
   渣里缩放——被固定深度实验取代。
2. **共享缓冲温和阶梯（E1-E4）**：阈值没贴顶→无效；E4=noecn 无 DCQCN，无意义。
3. **缓冲深度阶梯，阈值固定在上限 40%**：配错→全档饿死，反而收敛出"标记带
   必须贴队列上限"这条规律（现 §3.2 的核心）。
4. **mlxconfig `RDMA_SELECTIVE_REPEAT_EN` 旗标**：不改重传行为，实为 GBN；
   唯一正确的 SR 开启法是 ROCE_ACCL 寄存器（§2.1）。

---

## 7. 三个实验一句话结论（详见各图旁与 _archive）

- **1.2.1**：RDMA 份额几乎只由 ECN 标记带位置决定（0.3→22G 单调），
  GBN/SR 份额曲线几乎重合——重传算法不改分配；差异在别处（线上税、系统总吞吐）。
- **1.2.2**：同一工作点的时间序列——SR 两条直线到底，GBN 全程锯齿抖动
  （开机 3s incast 接管后被 DCQCN+丢包拉回，永不安定）。
- **1.2.3**：健康工作点上扫 CC 激进度——RDMA-fast+GBN 系统性崩溃（总吞吐
  塌到 5G、12 万 NAK），TCP-beta 成为强旋钮；SR 系统总吞吐在所有配置 ≥ GBN。
