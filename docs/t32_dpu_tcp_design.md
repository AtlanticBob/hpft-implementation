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

## D1 进展与关键未知(2026-07-05)

**已 de-risk:DPDK 与内核/OVS/RDMA 共存(PCI 功能共享)。**
`dpdk-testpmd -a 0000:03:00.1,representor=vf0 --disable-device-start` 探测 PF +
representor(探到 4 个 port:PF/uplink + representors),**探测后 RDMA 仍 5.57G、
ping OK**。mlx5 PMD 用 devx/verbs 自建队列,与内核共享同一 PCI 功能,不独占。

**steering 模型已看清(flow_switch_rss)**:DOCA Flow pipe 匹配流 →
`DOCA_FLOW_FWD_RSS` 到 SW 队列 0 → app 用托管 RX 收包。这正是把 vf0 TCP punt 到
Arm 的机制。

**剩余关键未知:eSwitch 编程是否与 OVS 共存。**
testpmd 探测用了 `--disable-device-start`(只 probe、不编程 eSwitch)。真正的 app
要往 eSwitch 加 steering pipe:
- 若 DOCA Flow **switch 模式**独占 eSwitch → 会顶掉 OVS,破坏 RDMA/其他 VF。
- 若能 **vnf/isolated 模式**或与 OVS-DOCA 共享 eSwitch → 非破坏性,只 punt 匹配流。
验证它必须跑一个会编程 eSwitch 的 app,有破坏已优化 RDMA 面的风险(需完整状态
备份 + 恢复流程:OVS 配置、tx/rx_agent、RP、underlay/SF 网)。

**OVS 已支持 DOCA 数据面**:`doca-openvswitch-switch` 已装,
`datapath_types: [doca, netdev, system]`,DPDK 26.03-doca4。当前跑 system(kernel)
+ hw-offload。若走 OVS-DOCA 共存,可把 underlay 桥切到 `datapath_type=doca`,再用
DOCA Flow 加 pacing punt pipe——这是生产可行的共存架构,但切换是对活跃系统的大改。

## D1 下一步(二选一,需权衡风险)

1. **谨慎验证 eSwitch 共存**:全量备份 RDMA 面状态 → 跑最小 DOCA Flow steering app
   (只加一条 vf0 TCP → SW 队列的规则)→ 观察 RDMA 是否存活 + 队列是否收到包 →
   立即恢复。决定 switch 模式能否共存。
2. **走 OVS-DOCA 路线**:先在低峰把 underlay 桥切到 doca 数据面并验证 RDMA/OVS
   正常,再在其上加 pacing punt pipe。更接近生产,但改动更大。

环境就绪:hugepages 2GB、DOCA 工具链(simple_fwd_vnf 编译通过)、flow_switch_rss
样例已定位、DPDK 探测端口模型已知。

## D1 路线 B 尝试:切 OVS-DOCA 失败(2026-07-05)

用户选路线 B(先切 OVS-DOCA 再加 pacing)。完整安全准备后尝试:
- 备份:conf.db.bak(pre-doca)+ 回滚脚本;SSH 走 rshim(tmfifo_net0)独立于数据口。
- 官方流程(NVIDIA docs):`other_config:doca-init=true` + `hw-offload=true` +
  `pmd-cpu-mask` + `dpdk-extra="-a PCI,representor=[...]"`;桥 `datapath_type=netdev`;
  端口 `type=dpdk options:dpdk-devargs=PCI,representor=[N]`;重启 OVS。
- 端口映射:p0=03:00.0、p1=03:00.1、pf1vf0-3=03:00.1(representor=vfN)、
  SF en3f*pf*sf0(representor=sf0)、host PF rep(representor=[65535])。

**结果:两次尝试均失败,OVS-DOCA 无法 bring-up 物理端口。**
- EAL/DOCA 初始化成功(DOCA 3.4/DPDK 26.03),representor 列表语法被接受。
- 物理端口 p0/p1 建默认 RSS pipe 失败:`Failed to get RX queue information -
  logical queue id 0 not exist` → `pipe 'OVS_RSS_PIPE_...' entry add failed,
  queue=0, rc=-22` → `Failed to create 'rx_ipv4_tcp' rss entry: Invalid input`
  → `Failed to init DOCA port p0` → `failed to add p0 as port: Invalid argument`。
- 加 `pmd-cpu-mask=0xF000`(PMD 队列分配确实开始跑)后,物理端口 RSS 仍失败,
  p1 侧 representors 变 `could not set configuration (Invalid argument)`。
- **排除 RP 冲突**:RP(doca_pcc -d mlx5_0)持有 mlx5_0=03:00.0=p0,而 **p0 侧
  成功、p1(03:00.1)侧失败**,故非 doca_pcc 占用设备所致。p0/p1 不对称原因未定
  (p1 带 4 个 VF,p0 不带 VF)。

**每次失败都干净回滚**(restore conf.db + 重建 kernel 桥):四 VF ping、RP
(4h+)、tx/rx agents、RDMA 5.57G、降速优化、TCP opt3 6.90G 全部完好。

**判定**:本系统(DOCA 3.4.0112 / fw 32.49 / OVS 3.4.0040)上 OVS-DOCA 的默认
uplink RSS pipe(rx_ipv4_tcp)无法建立——是 bring-up 层面的环境/版本问题,非我们
配置的表层错误(devargs/pmd-mask 都已按官方给足)。**路线 B 当前被此 blocker 卡住。**

## 选项1 深挖结果:OVS-DOCA HWS RSS bring-up 环境级失败(2026-07-05)

按用户建议深挖 + 网络搜索(NVIDIA 官方 docs/troubleshooting/论坛/known-issues)。
尝试过的组合(均失败于同一点):
- doca-init=true + hw-offload=true + hugepages(6GB)+ 正确端口顺序(PF 先于 VF)
- **pmd-cpu-mask=0xF000**(PMD 队列分配确实开始跑,日志有 "pmd to rx queue assignment")
- **dv_flow_en=2**(HWS 硬件 steering 模式,论坛指认的关键 flag)

**始终失败**:物理端口 p0/p1 建 OVS-DOCA 默认上行 RSS pipe 报
`hws_queue_mapping.c: Failed to get RX queue information - logical queue id 0 not
exist` → `rx_ipv4_tcp rss entry: Invalid input` → `failed to add p0 as port`。
p1 侧 representors 连锁 `Error attaching to DPDK`(PF port RSS 失败后 stop 所致)。

排查排除项:非 devargs 语法错、非端口顺序错、非 pmd 核缺失、非 HWS flag 缺失、
非 doca_pcc 设备占用(RP 持 mlx5_0=p0 恰是成功侧)。DOCA 3.4 known-issues 无此条。
FLEX_PARSER_PROFILE_ENABLE=0 / PROG_PARSE_GRAPH=False(基础 RSS 不应依赖这些)。

**判定**:本组合(DOCA 3.4.0112 / fw 32.49.1014 / OVS 3.4.0040)上 OVS-DOCA 的
uplink HWS RSS 默认流建不起来,是**环境/版本级 bring-up 缺陷**,非表层配置可解;
需固件层调查或换 OVS-DOCA/DOCA 版本或 NVIDIA 支持。**路线 B 在此环境不可行。**
每次尝试均干净回滚,RDMA(5.57G)+ 降速优化 + TCP opt3(6.90G)全程可恢复完好。

## 待决策(停止点)

1. **路线 A**(独立 DOCA Flow app,如 flow_switch_rss/simple_fwd_vnf 改造)——
   **反而可能可行**:自建 app 自己控制端口/队列/RSS 的建立,不走 OVS-DOCA 那条
   坏掉的 uplink RSS 路径,故大概率绕开本 bug。风险仍是与 OVS-kernel 的 eSwitch
   共存(需验证,testpmd probe 已证 PMD 层共存;编程 eSwitch 层未证)。
2. **固件/版本层**:调 mlxconfig(steering/flex-parser)或换 DOCA/OVS-DOCA 版本
   重试 OVS-DOCA。深水、不保证成、可能需重刷。
3. **重新评估**:host opt3 已是完整可用 TCP shaper(性能全达标),唯缺透明。透明化
   的基础设施摩擦已被证明很大。可权衡是否值得继续投 DPU 卸载。


## 路线 A 共存已证(2026-07-05,关键 de-risk)

用 testpmd 实测(可逆),回答了路线 A 的成败前提:
- **端口能启动**:`dpdk-testpmd -a 0000:03:00.1,representor=vf0,dv_flow_en=2` 启动
  端口成功(port2=uplink 0000:03:00.1 200Gbps,port3=vf0 representor 200Gbps)——
  **没触发 OVS-DOCA 那个 RSS bug**,证实该 bug 是 OVS-DOCA 特有、非硬件限制,
  独立 DPDK/DOCA app 能绕开。
- **与 OVS-kernel/RDMA 共存**:testpmd 运行期间 RDMA vf0=5.52G、vf1=185G 正常,
  host TCP 也正常;退出后全部恢复(vf0 RDMA 5.54G、TCP 7.07G)。**端口级共存成立,
  无性能回退、无冲突**(直接满足用户约束)。
- steering 待用 DOCA Flow API:HWS 模式(dv_flow_en=2)用异步 template flow API,
  testpmd 的经典 `flow create` 不生效(vf0 TCP 未被拦),故 steering 规则要用
  DOCA Flow(doca_flow_pipe,如 flow_switch_rss)编程,不能手搓 rte_flow。

**结论:路线 A 可行且非破坏。**端口映射:Port2=uplink(p1),Port3=vf0 rep。
下一步:基于 flow_switch_rss 写 DOCA Flow app —— 匹配 {vf0, TCP} → RSS 到 SW 队列
(punt 到 Arm),app RX→(D2 pacing)→TX 回 uplink;RDMA/非 TCP 默认走 OVS 不动。