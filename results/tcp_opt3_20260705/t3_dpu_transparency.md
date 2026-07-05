# T3.1:TCP shaping 卸载到 DPU 的可行性探针(2026-07-05)

目标:把 TCP enforcement 从 host(当前 opt3 TC-BPF 挂在 host VF netdev
`dpu1vf0` egress,租户内核可见)移到 DPU,达到 RDMA 侧 PCC 那样的完全透明。
逐一探测 DPU 侧可行的 shaping 点。三个决定性发现:

## 发现 1:host→network TCP 未被硬件卸载,走 Arm 软件 datapath

- DPU:`hw-offload=true`、switchdev、`hw-tc-offload on`,但 `ovs-appctl
  dpctl/dump-flows type=offloaded` 里 `10.1.0.x` 流 = **0 条**。
- host iperf 24.4 Gbit/s 时,representor `pf1vf0` 的 **sw rx_bytes 增长 25.5GB**
  (≈全部流量),物理口硬件计数几乎不动。
- 结论:这条流经 **OVS 内核软件 datapath**(24G 也印证非线速硬件转发),
  流量确实穿过 Arm 内核 → 理论上 DPU 侧内核 shaper 能看到它。

## 发现 2:representor 的 tc ingress 被 OVS 占用,mirred/ifb 劫持失败

- 试 `pf1vf0 ingress → mirred redirect → ifb → tbf/fq+EDT`(per-VF 透明 pacing
  的经典构造)。
- 结果:ifb 只收到 **830 字节**,host iperf 仍 25.3 Gbit/s 不受限。
- 原因:switchdev + OVS hw-offload 下,OVS 的 flower 规则占据 representor
  ingress 的高优先级 tc block,数据面在我的 matchall(pref 49152)之前就被
  OVS 处理/转发。representor netdev rx 计数增长,但我能挂的 clsact ingress
  钩子拿不到包。→ **在 OVS 拥有的 representor 上插 qdisc 型 shaper 不可行**。

## 发现 3:硬件 VF 限速(devlink function rate tx_max)在本固件上损坏 ⚠️

(这解决了 memory 里长期挂着的"VF 限速未决问题",结论是否定的。)

- `devlink port function rate set pci/0000:03:00.1/262145 tx_max <B/s>` 语法接受
  (rc=0),单位是**字节/秒**(`3gbps` 被当 3GB/s=24Gbit,是坑)。
- 但设置后 **vf0 vport 转发被彻底卡死**:host→sgpu02 与 sgpu02→host 双向
  100% 丢包;host tx 能到 pf1vf0 rx(+3 包),但 OVS FDB 学不到 vf0 MAC,
  包卡在 vport eSwitch 调度器出不去。
- 恢复尝试:`tx_max 0`(疑似被当 0 B/s 全阻断)、`tx_max` 高值、link bounce
  (host+representor)**均无效**;唯一有效恢复是 **OVS `del-port`+`add-port`
  pf1vf0 重编程 vport**。vf1/2/3(未动)全程正常,证明是 vf0 特异的 devlink
  损坏,非 fabric 问题。
- 结论:**本 DOCA 3.4/fw 32.49 上 devlink function rate 的 per-VF 硬件限速
  不可用且危险**(会 wedge vport)。不能作为透明限速原语。

## 选型结论(T3.2 方向)

| 方案 | 透明 | 语义 | 本环境可用? |
|---|---|---|---|
| host TC-BPF fq+EDT(opt3,现状) | ✗ 租户内核可见 | pacing ✓ | ✅ 已验证 |
| representor mirred→ifb→fq+EDT | ✓ | pacing | ❌ OVS 占用 ingress |
| devlink function rate(硬件 VF 限速) | ✓ | 硬件调度 | ❌ 固件损坏,wedge vport |
| OVS `ingress_policing_rate` | ✓ | **policing(丢包)** | ⚠️ 可用但违背"pacing 非 policing" |
| OVS QoS linux-htb on port | ✓(egress=网→host 方向) | shaping | ⚠️ 方向不对(限的是入向) |
| **DOCA/DPDK 例外路径 datapath** | ✓ | pacing ✓ | 🔨 需自建 DPU 用户态 datapath(大工程) |

**判断**:在当前 OVS-offload + switchdev + 本固件约束下,**没有现成的、既透明
又保持 sender pacing 语义的 DPU 内核/硬件挂载点**。要真正把 TCP pacing 卸到
DPU 且不退化为丢包 policing,只剩 **DOCA/DPDK 例外路径**这一条大路(自建 DPU
用户态 datapath,对 shaped 流禁用 offload、在 DPDK 里做 EDT pacing)。

**务实建议**:短期继续用 host 侧 opt3(pacing、per-pair、已充分验证)作为 TCP
enforcement;DPU 完全透明作为独立大工程按 DOCA/DPDK 例外路径推进。RDMA 侧之所以
能透明,是因为 PCC 跑在 DPA 上、直接控 NIC 的 dequeue rate,不经 OVS datapath;
TCP 没有等价的 DPA 卸载点,这是协议本质差异。

## 环境状态

探针结束后已完全还原:vf0 iperf 23G、四 VF 全通、host opt3(8G cap)已重挂。
devlink rate 未留任何绑定。
