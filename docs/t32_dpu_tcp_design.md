# T3.2:TCP shaping 卸载到 DPU 用户态 — 架构与实现计划(2026-07-05)

目标:把 TCP enforcement 从 host 内核(opt3 TC-BPF,租户可见)移到 DPU,达到
RDMA/PCC 那样的完全租户透明,且性能 ≥ 现有 opt3。

## 架构选型:硬证据排除了所有更简单的路线

| 候选 | 结果 | 证据 |
|---|---|---|
| host TC-BPF(opt3,现状)| 透明性 ✗ | 在租户内核 |
| representor ingress → mirred/ifb → fq+EDT | ❌ | OVS flower 占用 representor ingress,只截获 830B(T3.1)|
| **uplink p1 egress qdisc(tbf/fq+EDT)** | ❌ | **本次实测:p1 挂 tbf 3gbit,host TCP 仍 23.7G≈基线,无限速** |
| devlink function rate(硬件 VF 限速)| ❌ | fw 32.49 损坏,wedge vport(T3.1)|
| DOCA Flow 硬件 shaper | ❌ | DOCA Flow 只有 meter(policer/丢包),**无 delay-shaper** |
| **DOCA Flow RSS 例外路径 + DPDK 软件 pacing** | ✅ 唯一可行 | 见下 |

关键定论:**switchdev + eSwitch 下,VF→uplink 的包在硬件里搬运,内核 qdisc
(representor ingress 和 uplink egress 都试过)一律被绕过**;硬件又只有 policer
没有 shaper。所以既要透明、又要 delay 型 pacing(非丢包),**只能把包 steer 到
Arm 的 DPDK 队列做软件 pacing**。RDMA 不受影响(走 eSwitch 硬件,绕过内核 qdisc;
本次 p1 tbf 测试中 RDMA 仍 5.57G 正常)。

## 选定架构:DOCA Flow RSS 例外路径 + DPDK 软件 EDT pacing

```
host vf0 TCP egress
  → eSwitch: DOCA Flow pipe 匹配 {vf0, TCP} → RSS 到 Arm DPDK RX 队列(仅此流被 punt)
  → DPDK app(Arm):per-{src_ip,dst_ip} 软件 EDT pacing(timing wheel/日历队列,
     复刻 opt3 的 EDT 债务 + gap 旁路逻辑)
  → app TX → DOCA Flow pipe 转发到 uplink → 网络 → sgpu02
其余流量(RDMA、其他 VF、非 TCP)全程 eSwitch 硬件,不 punt,零影响。
```

- 硬件无 delay-shaper → pacing 在 DPDK 软件里做(和 opt3 同算法,只是从内核 fq
  搬到用户态定时队列)。
- 只 punt 目标 VF 的 TCP(例外路径),非破坏性——不动 RDMA uplink 绑定。
- 反向(network→host)不 pacing,直接转发或不 punt。

## 实现分阶段(D1-D4)

- **D1 数据面插入**:DOCA Flow steer vf0 TCP → Arm RX 队列;app 收到后原样 TX 回
  uplink(无 pacing)。验证:host TCP 透明连通 + 基线吞吐 ≥ opt3 的 ~24G。
  基座:`samples/doca_flow/flow_switch_rss`(RSS steering)+ `simple_fwd_vnf`(VNF
  转发结构)。**风险点:端口绑定不能抢走 RDMA 在用的功能;需谨慎管理 PCI/representor 绑定。**
- **D2 EDT pacing**:app 里加 per-pair 定时队列(日历/timing wheel),复刻 opt3:
  共享 pair EDT 债务 + inter-packet gap 旁路 + LRU flow 表。
- **D3 controller**:cap 从 hpft-unified-controller 下发到 app(共享内存/socket)。
- **D4 优化**:批量收发、多队列/多核、零拷贝,把吞吐/延迟/降速做到 ≥ opt3。

## 环境状态(已就绪)

- DPU DOCA 3.4.0112,DPDK + testpmd 就绪;hugepages 2GB 已分配。
- DOCA 工具链验证:`meson setup -Denable_all_applications=false -Denable_simple_fwd_vnf=true`
  + ninja 编译通过,产物 `/tmp/doca_build/simple_fwd_vnf`。
- DOCA Flow RSS 样例:`/opt/mellanox/doca/samples/doca_flow/flow_switch_rss`。
- host opt3(现网 TCP shaper)仍在 dpu1vf0 正常运行,D1-D4 期间不影响。

## 下一步:D1 第一小步

构建并运行 `flow_switch_rss` 样例,验证能把匹配流 steer 到 Arm RX 队列(不影响
RDMA);再把 vf0 TCP 的匹配 + TX-回-uplink 接起来,得到"透明直通"原型。
