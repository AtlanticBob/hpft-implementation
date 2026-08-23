# RDMA 操作类型覆盖：SEND/RECV 与 READ 在 HPFT 环路下的行为（2026-08-16）

## 问题

HPFT 迄今所有 RDMA 实验都用 `ib_write_bw`。机制上三个部件都不看 RoCE
opcode（接收端读 vport `received_ib_*` 字节、政策按 vNIC 流集合、PCC 按
QP/flowtag 限速），所以 SEND/RECV 与 READ 理应无需改代码即被覆盖。本实验
用同一个 incast8 场景把三种 verb 各跑一遍，回答两个问题：

1. SEND/RECV 是否与 WRITE 行为一致（预期：完全一致，网络上二者同构）。
2. READ 的数据方向反转（responder 发数据）后，PCC 是否对 READ response
   的 QP 触发事件并限速、rx 是否照常测量、公平是否成立（预期：成立，
   但此前无一手证据）。

## 拓扑与打流

- 数据面：常驻 VxLAN overlay，p1 发送 200G / 接收 100G，MTU 1500 VF。
- 交换机 swp37s0 绑 `motiv_default_ecn`（Kmin 400KB / Kmax 1.6MB / 20%，
  TC0+TC3），swp37s1 无 profile——评估计划 v3 §1.3 的默认态。
- 环境：`lab_env.sh hpft`（UPCC=1 两端、doca_pcc、rx/tx agent、pace-shim）。
- 8 个流集合：4 个 RDMA 对 + 4 个 TCP 对，直连 vf_n→vf_n（n=0..3），
  全部争抢接收端 97G 根；registry 8 VM 等权、类权重 tcp:rdma=1:1、
  MaxRate 50G（与 `tools/tests/incast8_regression.sh` 同）。
- 每对 RDMA 4 QP、64KB 消息、`-D 90`；每对 TCP `iperf3 -P4 -b0 -t85`。
- 数据方向三场都是 sgpu01→sgpu02。WRITE/SEND：perftest server 在 sgpu02、
  client（requester，发数据）在 sgpu01。**READ：角色互换**——server
  （responder，发 READ response）在 sgpu01、client 在 sgpu02，于是被
  PCC 治理的仍是 sgpu01 侧、hpft-dpu 后面的 QP，无需对称部署即可验证。
- 期望：8 流集合各 ≈11.5G（92G 有效根 / 8，headroom 0.08），Jain ≈1。
- 设备侧探针：运行期间每 2 s 向 RP mailbox 发 `0xdeb/0xdec <pair>` 回读
  {flowtag,budget,level,cc_rate,qp_count,cnp_hits,dbg_hits}。

## 结果

| verb | CC 项 | 场次 | 稳态窗 [40,85] Jain | 每流 (G) | 停摆 | 图 |
|---|---|---|---|---|---|---|
| WRITE | 软件 DCQCN（设备默认） | 4 | 1.000 / 1.000 / 0.983 / 0.964 | 干净场 RDMA 10.9–11.2 / TCP 11.1–11.3 | 2/4 出事（7 s、22 s…） | `fig_write_ctl.png` `fig_write_deep2.png` |
| WRITE | AIMD（`0xccd 0`） | 4 | **1.000 ×4** | RDMA 11.0–11.2 / TCP 11.1–11.3 | 0/4 | `fig_write_aimd1..4.png` |
| SEND/RECV | 软件 DCQCN | 1 | **1.000** | RDMA 11.1–11.2 / TCP 11.3 | 0 | `fig_send.png` |
| READ | 软件 DCQCN | 1 | 0.551 | 0–29 s 段 8 流全 11.2G | t≈29 停摆 → requester QP 超时死亡 | `fig_read.png` |
| READ | AIMD | 2 | **1.000 ×2** | RDMA 10.9–11.2 / TCP 11.2–11.3 | 0/2 | `fig_read_aimd3.png` `fig_read_aimd4.png` |

（"停摆"= 任一 RDMA 流集合 <3G 的秒；"健康段"= 稳态窗剔除停摆秒。）

### SEND/RECV：与 WRITE 完全一致，零代码改动

8 流集合 11.1–11.3G、Jain 1.000、全程无事件。PCC 探针 `qp_count=4`、
`dbg_hits` 增速与 WRITE 同量级。**结论：SEND/RECV 已被支持，只是此前
没测。**

### READ：机制成立；在 07-28 的 CC 项下与 WRITE/SEND 同等

- **机制成立**：READ 的数据由 responder 发出，本实验把 perftest server
  （responder）放 sgpu01、client 放 sgpu02，数据方向仍 sgpu01→sgpu02，
  治理点就是 hpft-dpu 的 PCC。探针显示 READ responder 的 QP 被 PCC 完整
  识别：`qp_count=4`、`dbg_hits` 每 2 s +~70 万（ROCE_TX 事件在流上）、
  `cnp_hits` 随拥塞增长（CNP 由 requester 网卡固件回、被 responder 侧
  PCC 匹配到 pair）；rx_agent 把 READ response 字节正常记入 `|rdma`
  流集合。**这回答了唯一的硬件不确定点：BF3 PCC 对 READ response 触发
  事件并接受限速。**
- **AIMD 项下 2/2 干净**：Jain 1.000、8 流全 11.2G、零停摆，与 WRITE
  AIMD 4/4 一致——READ 已被支持，条件与 WRITE 相同。
- **软件 DCQCN 项下 READ 对停摆零容忍**：WRITE 里被限速的是 requester
  自己，包被压在网卡调度里、ACK 定时器不启动，停摆几秒只是不发；READ
  里被限速的是 responder，requester 早已发出请求在等响应，响应停
  >~0.5 s（perftest 默认 timeout=14 × retry=7）就 `transport retry
  counter exceeded (syndrome 0x81)`，RC 终态。**同一个停摆，对 WRITE
  是几秒吞吐损失，对 READ 是连接死亡。**所以 READ 能否宣称支持，完全
  取决于附带发现 1 里 CC 项的选择。

### 附带发现 1：RDMA 执行面的饿死环——软件 DCQCN 项 × 工作保守重分配 × TCP 造成的标记

**现象。** incast8 稳态窗里 RDMA 流成批停摆几秒到几十秒：WRITE 对照
t=74–80（7 s）、READ t=30–37（随后 QP 死亡）、SEND 零次。07-28 的
`results/regression/final` 在同一判据下零停摆、每流最低 11.6G。

**A/B 定因（12 场 write incast8，同一 runner，同一 registry）。**

| 臂 | 交换机 ECN（swp37s0） | CC 项 | 出事/总数 | 停摆秒 | Jain |
|---|---|---|---|---|---|
| 深阈值 | motiv_default_ecn 400K/1.6M/20% | 软件 DCQCN（设备默认） | 2/4 | 7, 55(READ) | 0.983 / 0.551 |
| 浅阈值 | ecn_incast_bzx 108K/396K/20%（07-28 条件） | 软件 DCQCN | 2/4 | 22, 9 | 1.000 / 0.964 |
| 深阈值 | motiv_default_ecn | AIMD（07-28 的项，`0xccd 0`） | **0/4** | — | 1.000 ×4 |

（深阈值臂的 4 场 = write 对照、write 第 2 遍、send、read；浅阈值臂
CNP 总量高 10–40 倍但停摆率不变。）

**结论。** ECN 阈值深浅与停摆无关；把 CC 项切回 07-28 的 AIMD 立即
消失（write 4/4、read 2/2 干净）。极限环是 08-04 把设备默认 CC 项从
"形同虚设的弱兜底"换成忠实的 DCQCN 状态机之后出现的**回归**。

**真正的环（探针 + rx 记录）。** 不是 N 学习。设备码里已加 N 保持
（`qp_hold`：N 即时升、每 32 个 epoch 才降 1，mailbox `0xcce <v> 3`
可调），write 4 场仍 2 场出事、read 2 场全停摆——N 起初被保住了 4，
但流照样归零。write_nhold3 停摆期 pair 0 的状态：`cc_rate=2`（最低值）
持续 13 s 以上、α≈0.5–0.8、恢复目标 `Rt=2` 也被拖到底，而 `cnp_hits`
以 ~190/s 持续增长——**RDMA 几乎不发包了，CNP 还源源不断**。同一时刻
rx 记录：t=46 四对 RDMA 同时归零，TCP 从 45G 跳到 82G——水填充把 RDMA
没用的份额工作保守地转给了 TCP，TCP 以 ~80G 加 Cubic 突发把交换机队列
顶在 Kmin 之上，RDMA 的涓流每个包都被标记，DCQCN 钉死在最低值：
**自维持的饿死**。这就是 motiv 1.2 里"DCQCN × Cubic → RDMA <1%"的
现象在 HPFT 内部重现，因为 `rate=min(cc_rate, level)` 允许一个忠实的
DCQCN 用 TCP 造成的标记推翻政策 level，而重分配又把 TCP 推得更满。
入环的触发（4 对同时崩）是随机的交换机级 CNP 风暴（队列瞬间过 Kmax），
出环靠运气。

**这不是实现缺口，是设计层面的相互作用**（CC 项 × 工作保守重分配 ×
共享有损队列里的非 ECN TCP），需要设计侧裁定：(a) 生产默认 CC 项回到
弱兜底（07-28 已验证形态），DCQCN 项只作 CC 矩阵臂并注明此现象；
(b) cc_rate 下限按 level 的比例设地板，限定饿死深度；(c) 重分配不吃
RDMA 因拥塞而空出的份额。

### 附带发现 2：ARP 竞态把 RoCE 送错 VF（lab 缺陷，已修）

overlay 让一台 host 的 4 个 VF 处在同一个 L2 广播域，内核默认
`arp_ignore=0` 时对 10.1.i.x 的 ARP 请求 4 个 VF 各答一份自己的 MAC，
对端留下最后到的一份。本次 sgpu01 上 10.1.2.2/10.1.3.2 都指向 sgpu02
**vf1** 的 MAC：ICMP/TCP 靠弱主机模型照通，RoCE 帧被送到 vf1 的网卡、
GID 表没有该 IP、硬件静默丢弃——"QP 建连成功、0 次迭代"。修法：两台
host 的 VF 接口 `arp_ignore=1 / arp_announce=2` 并清 neigh；已写进
`tools/lab-infra/vf_setup.sh`、`tools/cc_mode.sh post_recover`、
sgpu02 的独立副本 `~/hyperfront/vf_setup.sh`。`flow_preflight.sh` 原来
把 0 带宽判为 ok，现改判 DEAD。sgpu02 上 iperf3 服务只有 5301/5302，
5303/5304 缺失（incast8 假定四个都在），已补起。

### 附带发现 3：sgpu01 VF 的 responder 发送僵死（lab 缺陷，可复原）

第一场 read（requester 端 QP 超时死亡、responder 端随后被杀）之后，
sgpu01 四个 VF 作为 **responder** 的一切发送（ACK、NAK、READ response）
停止：包收进来（`rx_read_requests` 增、`port_rcv_packets` 增）、QP 状态
正常（RTR）、无 responder 错误计数、`port_xmit_packets` +0；作为
requester 的发送完全正常（26.6G）。PCC 进程停掉也一样，neigh/GID 均
正确——问题在 host 侧 VF 状态，**重建 sgpu01 VF 即恢复**（重建后 READ
responder 26.7G）。触发机制未定位。它让两场 read（`read_nhold1/2`）
和两场 `read_aimd1/2` 从头全零——这四场作废，不计入任何结论。runner
现对 read 场景做 responder 方向的预检，死则重建一次 sgpu01 VF（并恢复
fq+EDT、pace-shim），仍死则中止。

## 判定

- **SEND/RECV：正向，高信心。** 现有实现已支持，可以直接说"WRITE 与
  SEND 同等"，有图有数。
- **READ：正向，高信心（条件同 WRITE）。** 数据方向反转不需要改任何
  agent/PCC 代码，硬件事件路径通，AIMD 项下 2/2 Jain 1.000。真实部署
  形态（每台 DPU 同跑 rx+tx）与本实验等价，因为治理点始终是发数据的
  那台 DPU。论文可以宣称 WRITE/SEND/READ 三者同等，前提是执行面 CC 项
  取 07-28 形态。
- **软件 DCQCN 项下的饿死：负向，设计层面待裁定。** 它不只是 READ 的
  问题——WRITE 在稳态窗里丢过 7–39 s。三个方向见附带发现 1。在裁定前，
  设备默认仍是软件 DCQCN（与评估战役一致），实验 runner 用 `CC_ALGO=0`
  取 AIMD。设备码里的 N 保持已部署（repo == DPU），无害但不是修法。

## 产物

- `incast8_ops_run.sh <write|send|read> <tag>`：runner（read 模式角色互换、
  设备侧探针、产物进 `results/`）。
- `analyze.py <tag> [title]`：指标 + 图（`fig_<tag>.png/.pdf`），含停摆秒
  与健康段。
- `results/<tag>_{rx,tx}.jsonl`、`_rp.txt`（探针）、`_pt_n.log`（sgpu01
  perftest）、`_peer_n.log`（sgpu02 perftest）、`_iperf_n.json`、
  `_report.txt`。
- `summarize_ab.py tag:label ...`：跨场表（停摆秒、Jain、CNP 总量/最大
  2 s 突发）；`switch_ecn_ab.sh define|shallow|deep|status`：交换机
  swp37s0 的 ECN 臂切换（只动我们的口）。
- `nseries.py <tag>...`：探针里 N（qp_count）的直方图；`switch_ecn_ab.sh`。
- lab 停留态：HPFT 环境（UPCC=1、agents 运行、standing registry 已恢复）；
  交换机 swp37s0 绑回 `motiv_default_ecn`（评估计划默认），
  `ecn_incast_bzx` 已按 rev 373 原定义重建（rev 387/388）；PCC 设备码
  含 N 保持（`qp_hold`），CC 项默认软件 DCQCN（runner 的 `CC_ALGO` 只在
  单场内生效，每场 RP 重启后回默认）；sgpu01 VF 于 14:5x 重建过一次
  （MAC 不变），fq+EDT 与 pace-shim 已恢复。
