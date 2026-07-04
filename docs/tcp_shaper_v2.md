# TCP shaper v2(2026-07-04)

设计目标与 RDMA v2 相同:租户透明、发送端 pacing(非丢包)、粒度
{src_vnic, dst_vnic}、共享 cap、活跃流高频改速。

## 为什么 TCP 没有 RDMA 那样的统一硬件抓手

RDMA v2 的突破是 DOCA PCC —— RNIC 的 QP scheduler 是 VF 边界以下的硬件
pacing 旋钮。**TCP 没有等价物**:TCP 的拥塞/发送状态在租户 kernel 里,NIC
上没有可编程的"TCP 发送调度器"。因此 TCP 的透明发送端 pacing 只有两条路:

1. **host kernel TC-BPF EDT(本阶段验证)**:在发送 host 的 VF netdev egress
   挂 TC-BPF,按 {src_ip, dst_ip}→pair 查 pinned map 的速率,写 skb 的
   Earliest Departure Time,由 sch_fq 执行 pacing。对 **VM-passthrough 租户
   透明**(BPF 在 hypervisor host 的 VF netdev,VM 内不可见);对 host 直接用
   VF 的场景不透明。
2. **DPU 用户态数据面 EDT(设计,未实现)**:切 OVS-DOCA/DPDK,被 shape 的
   pair 走 eSwitch 例外规则进 Arm PMD,软件 EDT pacing 后从 uplink 发出,
   未 shape 的保持硬件 offload 线速。**真正租户透明**(执行在 DPU),但需要
   改 OVS/写 DPDK pipeline。当前 lab 的 OVS datapath_types 已含 `doca`/`netdev`,
   路径可行。

## 本阶段验证(host TC-BPF EDT,复用 v1 已验证原语)

机制:`hpft_tcp_edt_kern.o`(TC egress classifier)+ pinned maps
(IP→vNIC、pair→rate)+ root `fq`。控制:`tcp-shaper-apply`(装载+填 map)、
`tcp-shaper-update-rate`(改一条 pair 速率 + generation bump)。

| 场景 | 结果 |
|---|---|
| TCP 基线(vf0→vf0,无 shaping) | 23–25 Gbps |
| IP 不匹配(fail-open) | 21.6 Gbps(放行,正确) |
| 8G cap | **8.00 Gbps** |
| 动态改速 8G→3G(活跃流) | 稳定 **3.00 Gbps**,TCP 不断连 |
| 共享 cap(same-pair N streams) | v1 已验证聚合守 cap |

数据面机制:每包/大 skb 查可变 per-pair 速率状态,写 EDT 时间戳,fq 执行;
控制面改速只替换一个 map 值并 bump generation,活跃流下一个包即用新 cap——
与 RDMA v2 的"每事件读可变 pair 速率状态"是同一思想在 TCP 栈的落地。

## 与统一控制面的对接

- registry 已含 `tcp_rules`(`config/lab-tcp-registry.json`,当前 lab IP)。
- `hpft-shaper-controller` 未来扩展:RDMA 规则驱动 PCC/mailbox,TCP 规则驱动
  TC-BPF EDT(host)或 DPU 用户态(将来),同一 {src_vnic, dst_vnic, rate_bps}
  接口、同一 JSON-lines 运行时改速。这实现了 v1 完成评审里"输出控制形状对齐
  RDMA"的目标。

## 结论

TCP shaper v2 的 EDT 原语在当前 v2 环境验证可用,达成全部设计目标(用与
RDMA 不同的机制)。透明性上,host TC-BPF 对 VM 租户透明且立即可用;完全
DPU 侧透明(offload 例外路径)是已设计、路径可行、待实现的下一步。这与调研
报告的结论一致:RDMA 有统一硬件抓手(PCC),TCP 没有,分协议实现可接受。
