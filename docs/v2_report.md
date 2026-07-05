# hpft-shaper v2 总收官报告(2026-07-05)

透明发送端速率限制,粒度 `{src_vnic_id, dst_vnic_id}`,RDMA + TCP 双协议。
本报告汇总 v2 的全部成果、与立项设计目标的逐条对照、以及唯一未闭合的
透明性缺口(TCP 卸载到 DPU)及其技术根因,供决定是否投入 T3.2 / T4。

---

## 1. 立项目标与 v1 缺陷

v1(`hpft-exp`:patched rdma-core + host TC-BPF EDT)能限速,但**执行点在租户
可见的位置**(改过的 rdma-core 库、租户主机内核 TC),不满足云多租户透明要求。
v2 的目标:执行点下沉到 VF 边界以下 / DPU,租户零改动零感知,同时保持:

1. 统一策略粒度 `{src_vnic, dst_vnic}`(协议无关的键)
2. 租户透明(enforcement 不在租户可见面)
3. **发送端 pacing**,不是丢包型 policing
4. 一个 cap 跨多 QP / 多进程 / 多 flow 共享
5. 响应高频速率变动

---

## 2. RDMA 侧:完全达成(透明)

DOCA PCC on BF3 DPA(SCR / White-Boxing RDMA, NSDI'25 路线)。执行全部在 DPA
硬件 QP scheduler 的 dequeue-rate,host / 租户零改动。

| 设计目标 | 达成 | 证据 |
|---|---|---|
| 统一粒度 {src,dst} | ✓ | flowtag→src function;qpn override→dst;单源两 dst 6G/2G 独立 ≤0.2% |
| 租户透明 | ✓ | 执行在 DPA;host 全程无安装无配置 |
| 发送端 pacing 非丢包 | ✓ | RNIC QP scheduler dequeue-rate,非 policing |
| 多 QP/进程/flow 共享 cap | ✓ | 1024 QP 聚合 +0.3%;pair 级预算,与流数无关 |
| 高频改速 | ✓ | cap 传播 394ms(单钟);10Hz 干净跟踪;mailbox 13.2ms/75Hz |

- **Phase 1 GO**(`docs/phase1_report.md`):VF/PF 均受控、精度 ≤3.2%、fail-open。
- **Phase 2 A/B/C 完成**(`docs/phase2_report.md`):A=多 dst qpn override;
  B=DCQCN `rate=min(cc,level)`(冻结注入已证,真实 CNP 待 ECN/incast);
  C=统一 controller + registry,端到端 6.06G。
- 架构:接收端 `rx_agent` 测 per-dst representor 速率(精确 R)→ UDP → 发送端
  `tx_agent` → 批量 mailbox → DPA 水位积分控制,`rate = min(cc_rate, level)`。

---

## 3. TCP 侧:机制达成(pacing/cap/多流/高频),透明性未闭合

TC-BPF EDT(Earliest Departure Time)+ `sch_fq`,挂在发送端 egress。opt3 v3 为
默认实现(`tcp/bpf-opt3/`,`results/tcp_opt3_20260705/`)。

### 3.1 opt3 v3 机制(定稿)

- **聚合限速 = 共享 per-pair EDT 债务延迟**:所有包累加 `pair.next_ns`,bulk 流
  读它当 `skb->tstamp` 被 fq 平滑 pace。→ cap 精确、多流经 fq 天然公平、
  降速立即生效、**不丢包**(delay 非 drop)。
- **latency 解耦 = inter-packet gap 旁路**:flow(5-tuple)距上次发包 gap>40µs
  ⇒ "发一个等一个"的 latency 流 ⇒ 旁路共享 pacing 延迟(字节仍计入 pair 债务,
  cap 不漏)。**任意包大小**都有效。
- **flow 状态 = LRU_HASH**:死流自动老化,满表不会把 bulk 误判 sparse。

演进中踩过的两个坑(都是负优化,已否决):opt2(小包旁路,判据是包大小)漏掉
大包 latency 流(3-4KB 请求 985µs);opt3 v2(per-flow 债务 + pair 债务超 horizon
丢包)在 4 条竞争 bulk 流下**丢包塌陷 8G→1.1G、严重不公平**。

### 3.2 验证数据

| 度量 | baseline | opt2 | opt3 v2 | **opt3 v3** |
|---|---|---|---|---|
| 4 bulk 流同 pair 聚合(cap 8G) | 8.36G | 8.36G | 1.11G ✗ | **8.15G ✓** |
| 4 流 per-flow 公平 | ~2G | ~2G | 极不公平 ✗ | **~2G ✓** |
| saturated 小包 lat p50 | 1578µs | 70µs | 65µs | **66.9µs** |
| saturated lat @64B..64KB | — | 大包 985µs ✗ | — | **65–78µs 全好 ✓** |
| cap 精度 / 饱和吞吐 | — | — | — | **7.02G / 6.86G** |
| 降速响应(15ms 窗口口径) | 22.8ms | — | — | **22.8ms(真实拐点 ~7ms)** |

### 3.3 控制路径与速率变动(`results/tcp_rate_perf_20260705/`)

- 控制路径必须**常驻进程 + direct bpf() map 写入**:in-process **48µs**(高频可用);
  CLI 工具每次 fork 136ms 不可用。
- 速率变动延迟只用**降速**测量(升速是 TCP cwnd 收敛,与 shaper 无关)。真实
  降速数据面响应 **6–7ms**(≈140Hz);连续 30ms/步阶梯干净跟随。

### 3.4 TCP 达成对照

| 设计目标 | 达成 | 说明 |
|---|---|---|
| 统一粒度 {src,dst} | ✓ | pair_key = f(src_ip,dst_ip);registry 键对齐 RDMA |
| 发送端 pacing 非丢包 | ✓ | EDT+fq 延迟;opt3 v3 全程不丢包 |
| 多 QP/进程/flow 共享 cap | ✓ | 共享 pair 债务,多流 8.15G 公平 |
| 高频改速 | ✓ | 控制 48µs;降速 6–7ms/140Hz |
| **租户透明** | **✗** | **enforcement 在 host VF netdev egress,租户内核可见** |

---

## 4. 统一控制面(达成)

`tools/hpft-unified-controller`:一个常驻进程、一套 `{src_vnic,dst_vnic,rate_bps}`
JSON-lines 协议,协议无关分发:

- `proto=tcp` → in-process `DirectBpfMapWriter` 直写 pinned opt3 map,稳态
  **28–38µs,永不 fork**。
- `proto=rdma` → PCC receiver-cap 文件(rx_agent 热读)。

活体验证:TCP 逐档降速(10/6/3/1G)数据面跟随,控制延迟 <100µs。

---

## 5. 唯一未闭合缺口:TCP 卸载到 DPU 的透明性(T3.1 探针,`t3_dpu_transparency.md`)

RDMA 透明是因为 PCC 跑在 DPA 上、直控 NIC 的 QP dequeue rate,**不经 OVS
datapath**。TCP 没有等价的 DPA 卸载点。逐一探测 DPU 侧挂载点,三个决定性发现:

| DPU 挂载点 | 透明 | 语义 | 本环境可用? |
|---|---|---|---|
| host TC-BPF fq+EDT(opt3,现状) | ✗ 租户可见 | pacing ✓ | ✅ 已验证 |
| representor mirred→ifb→fq+EDT | ✓ | pacing | ❌ OVS flower 占用 ingress(只抓到 830B) |
| devlink function rate(硬件 VF 限速) | ✓ | 硬件调度 | ❌ **固件损坏:wedge vport,需 OVS 重编程恢复** |
| OVS ingress_policing_rate | ✓ | **policing(丢包)** | ⚠️ 违背"pacing 非 policing" |
| **DOCA/DPDK 例外路径 datapath** | ✓ | pacing ✓ | 🔨 需自建 DPU 用户态 datapath(大工程) |

- host→net TCP 在本环境**未硬件卸载**(走 OVS 软件 datapath,representor sw rx
  增长 25.5GB / 24G iperf),流量确实穿 Arm 内核,但 representor 的 tc ingress
  被 OVS 占用,劫持不了。
- **硬件 VF 限速(devlink function rate)在 fw 32.49 上损坏**:设 tx_max 后 vf0
  vport 双向 100% 丢、包卡 eSwitch 调度器、FDB 学不到 MAC;`tx_max 0`/高值/link
  bounce 均无法恢复,唯一恢复 = OVS `del-port`+`add-port`。这给长期挂着的
  "VF 限速未决"画句号:**否定**。(探针后已完全还原。)

**判断**:当前 OVS-offload + switchdev + fw 32.49 下,没有既透明又保持 sender
pacing 语义的现成 DPU 挂载点。真正卸载只剩 **DOCA/DPDK 例外路径**(对 shaped 流
禁用 offload、在 DPDK 里做 EDT pacing),是独立大工程。

---

## 6. 总体对照与建议

| 设计目标 | RDMA | TCP |
|---|---|---|
| 统一粒度 {src,dst} | ✓ | ✓ |
| 发送端 pacing 非丢包 | ✓ | ✓ |
| 多 QP/进程/flow 共享 cap | ✓ | ✓ |
| 高频改速 | ✓(13ms/75Hz) | ✓(48µs 控制 / 6–7ms 降速) |
| **租户透明** | **✓(DPA)** | **✗(host TC-BPF;DPU 路径见 §5)** |

**结论**:五条目标 RDMA 全达成;TCP 达成机制四条,**透明性一条待 DOCA/DPDK
例外路径**。近期生产可用形态 = RDMA PCC(透明)+ TCP host opt3(pacing,机制完备,
待透明化)+ 统一 controller。

**剩余大投入(待决策)**:
- **T3.2**:DOCA/DPDK 例外路径,把 TCP pacing 真正卸到 DPU 用户态、完全透明。多日,
  不保证成。是唯一能闭合 TCP 透明性缺口的路线。
- **T4**:真实 CNP 触发(ECN/incast)、1024-QP 级 RDMA 压测、TCP 极低 cap 下
  gap 阈值标定。硬化现有实现。

---

## 附:关键工件

- RDMA:`pcc/device/hpft_rp_main_v2.c`、`tools/dpu/{rx_agent,tx_agent2}.py`、
  `docs/phase1_report.md`、`docs/phase2_report.md`
- TCP:`tcp/bpf-opt3/hpft_tcp_edt_kern.c`、`results/tcp_opt3_20260705/`
  (含 opt3 定稿、统一 controller、T3 透明性三份)、`results/tcp_rate_perf_20260705/`、
  `results/tcp_latency_20260705/`
- 控制面:`tools/hpft-unified-controller`、`config/lab-registry.json`、
  `config/lab-tcp-registry.json`
