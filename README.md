# HPFT Shaper v2

新一代发送端 shaper。目标与 v1(`hpft-exp`)一致,但执行点全部移到租户不可见的位置
(VF 边界以下:NIC 调度器 / DPU),解决 v1 的透明性缺陷。

## 设计目标(不变)

1. 统一策略粒度 `{src_vnic_id, dst_vnic_id}`。
2. 租户应用零修改,正常使用 RDMA verbs / TCP socket,且**感知不到 shaper 的存在**。
3. Provider 侧发送端 shaping(pacing),不靠丢包 policing。
4. 同一 pair 下多 QP、多进程、多 TCP flow 共享一个总 cap。
5. 活跃长流可响应高频(≥100 Hz)限速值更新。

## 架构决策(2026-07 调研结论)

- **RDMA:DOCA PCC on DPA(SCR 路线)**。RNIC 的 QP scheduler 本身就是硬件
  rate limiter;DPA 上的 PCC 算法为每个 flow 设置 dequeue rate,数据路径留在硬件。
  参考:White-Boxing RDMA (SCR), NSDI'25。
- **TCP:DPU Arm 用户态数据面 + EDT**(后续 phase)。被 shape 的 pair 走
  OVS-DOCA/OVS-DPDK 例外路径进 Arm,软件 EDT pacing(移植 v1 已验证的逻辑);
  未被 shape 的流量保持 eSwitch 硬件 offload 线速。
- 两个 enforcer 之上是同一个 controller,键为 `{src_vnic, dst_vnic}`,沿用 v1 的
  registry 与高频更新协议。

## 当前阶段

Phase 1:DOCA PCC 可行性探测。见 `docs/phase1_pcc_probe_plan.md`。

## 仓库布局

- `docs/`:计划、设计、探测报告。
- `pcc/`:PCC 探测与后续 RDMA shaper 代码(DPA device 侧 + Arm host 侧)。
- `results/`:实验原始输出与 summary,目录名 `<probe>_<UTC日期>`。

## 环境速查(2026-07-03 确认)

| 项 | 值 |
|---|---|
| 本机 | sgpu01;VF `dpu1vf0..3` = 10.1.{0..3}.1/24,RDMA 设备 mlx5_6..9 |
| 对端 | sgpu02(ssh 直达);VF 同构,10.1.{0..3}.2,vf0 = mlx5_6 |
| DPU | `ssh hpft-dpu`(192.168.102.2,ubuntu,passwordless sudo) |
| DOCA | 2.9.3008(Arm);PCC 参考应用 `/opt/mellanox/doca/applications/pcc/`(rp+np) |
| DPA 工具链 | `/opt/mellanox/doca/tools/`:dpacc、dpa-clang、dpa-gdbserver、dpa-ps 等 |
| 固件 | 32.47.2682;`USER_PROGRAMMABLE_CC=1` 已开启(无需扰动性 mlxconfig/fw reset) |
| perftest | 两端都用 `~/hyperfront/perftest-26015/ib_write_bw`(系统 6.23 版会 crash) |
| RDMA 基线 | vf0↔vf0 RDMA WRITE 196 Gb/s(65536B,单 QP) |
