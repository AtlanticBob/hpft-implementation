# S0 环境盘点与回滚 SOP(2026-07-03)

## DPU(hpft-dpu = sgpu01 的 BF3,192.168.102.2)

- 固件 32.47.2682;`USER_PROGRAMMABLE_CC=1` 已开启(mst 设备 `mt41692_pciconf0`)。
- DOCA 2.9.3008;meson 0.61.2 / ninja 1.10.1;dpacc 工具链完整
  (`/opt/mellanox/doca/tools/`:dpacc、dpa-clang、dpa-gdbserver、dpa-ps、dpa-statistics)。
- Arm 侧 RDMA 设备:
  - `mlx5_0` = p0(PCI 03:00.0),`mlx5_1` = p1(PCI 03:00.1)——PF,PCC 的目标设备;
  - `mlx5_2/3` = bf-rdma0/1;SF netdev `en3f0pf0sf0` / `en3f1pf1sf0`。
  - host 侧 VF(dpu1vf0..3)挂在 host PF1 上,物理口 p1。**PCC 探测目标 = `mlx5_1`**。
- 物理口 p0/p1 均 200 Gb/s。
- PCC 参考应用:`/opt/mellanox/doca/applications/pcc/`
  - host 侧:`host/pcc.c` + `host/pcc_core.c`(argp CLI);
  - device 侧:`device/rp/rtt_template/`(RP RTT 模板算法)+ `device/np/`;
  - **已有预构建二进制** `/opt/mellanox/doca/applications/build/pcc/doca_pcc`
    (2025-10-29 构建,aarch64,--help 正常);
  - 默认模式(无 telemetry flag)= RP RTT 模板;`-d <ibdev>` 必填,`-w` 运行时长
    (负值=常驻),`-t` 指定 DPA 线程(默认取 pcc_params.json 里的 EU 列表)。
  - meson 选项:`enable_pcc`(默认 false)、`enable_pcc_application_tx_counter_sampling`、
    `enable_pcc_application_np_rx_rate`(均默认 false)。

## 两端 host

- sgpu01(本机):VF `dpu1vf0..3` = mlx5_6..9,IP 10.1.{0..3}.1/24。
- sgpu02:对称,vf0=mlx5_6(10.1.0.2)、vf1=mlx5_7(10.1.1.2);ssh 直达。
- perftest:两端 `~/hyperfront/perftest-26015/`(ib_write_bw/lat 等 8 个二进制);
  系统 /usr/bin 的 6.23 版会 core dump,勿用。
- 基线:见 `results/s0_baseline_20260703/summary.md`(全 case 196 Gb/s 线速,
  lat typical 2.40 µs)。

## 回滚 SOP

PCC 的作用方式:`doca_pcc` 进程存活期间,其 DPA 算法接管该设备(整卡该端口,
含所有 host VF)的 RoCE CC 速率决策;进程退出(正常退出、kill、或 `-w` 超时)
后固件默认 CC(DCQCN)自动恢复,无持久状态。

1. 正常回滚:DPU 上 `pkill -f doca_pcc`(或等待 `-w` 超时自退)。
2. 验证:重跑 `tools/run_s0_baseline.sh` 的任一 bw case,应回到 ~196 Gb/s;
   延迟 case 回到 ~2.4 µs。
3. 异常状态(进程 kill 后流量仍异常 / 设备报错):
   - `dpa-ps` 查看残留 DPA 进程,`dpaeumgmt` 检查 EU 状态;
   - 最坏情况 `sudo mlxfwreset -d /dev/mst/mt41692_pciconf0 reset`
     (扰动 host 侧所有 function,需 maintenance window,先与 lab 协调)。
4. 实验纪律:doca_pcc 运行段用 `-w <秒>` 给定自退时长作为保险,避免常驻
   进程被遗忘;每段运行的起止时间记入 results 的 summary。

## 与计划的偏差

- S1 预期"构建应用"——实际发现镜像自带预构建 `doca_pcc`,S1 改为:先用预构建
  二进制完成运行/回滚验证,同时把源码拷入本仓库供 S2 修改重编(构建路径已验证
  存在:顶层 `meson -Denable_all_applications=false -Denable_pcc=true`)。
