# Motivation 1.2 实验运行日志（2026-07-14）

目标：32 流（4 VF pair × 4 RDMA + 4 TCP）过 100G 瓶颈（swp37s0 egress），
证明异构 CC（DCQCN vs Cubic）在 incast 下收敛结果由算法设计细节决定。
子实验 A：ECN 标记参数扫描（激进 25K/100K/80%、中性 100K/400K/20%、
温和 400K/1600K/5%）。子实验 B：速率恢复激进度（RDMA rpg_time_reset/
ai/hai；TCP cubic beta 410/717/922）。每个点 × {GBN, SR}。

## 环境定案

- 固件原生 DCQCN：发现 lab 长期处于 `USER_PROGRAMMABLE_CC=1`（PCC 停掉后
  固件回落到内部 DPA 算法，不是 DCQCN：表现为贴线速零丢包、无视一切
  ECN/CNP/RP 参数）。已把双端 DPU `USER_PROGRAMMABLE_CC=0` +
  `RDMA_SELECTIVE_REPEAT_EN=0` 并用 `mlxfwreset --sync 1` 激活。
  **恢复 lab 时必须设回 =1 并再次 fw reset，否则 E 栈 PCC 起不来。**
- Arm `reboot` 不激活 mlxconfig；必须走 host 侧
  `echo 0 > sriov_numvfs; mlxfwreset -d 38:00.0 --yes --sync 1 reset`
  （activate-fw.sh 的流程），Arm 会随之重启，~4 分钟。
- 瓶颈：dpu2 p1 ethtool 100G（交换机 swp37s0 随之 100G）；发送侧 200G。
- lossy：双端 DPU p0/p1 global pause off + PFC 全零（fw reset 后要重设）。
- host 侧残留清理：sgpu01 四个 VF 上的 hpft_tcp_edt BPF filter 与 fq root
  qdisc 已删，恢复默认 mq+fq_codel；VF MTU 钉 1500（与上一轮动机实验一致）。
- TCP：cubic，`tcp_ecn=2`（客户端不请求 ECN，即 TCP 无 ECN），无任何调优。
- 同 TC 验证：烟测中 RoCE 与 TCP 流量都只推 TC0 计数器（TC3 纹丝不动），
  RoCE 数据帧自动带 ECT(0)，TCP 不带 ECT。
- E 栈（tx/rx agent、pace-shim、PCC RP）全停；soak cron 已注释
  （#MOTIV12# 标记）；dpu1 hpft-txagent.service 本就 disabled。

## 时间线

- 18:10 起环境核查与清场。
- 18:3x 发现 USER_PROGRAMMABLE_CC=1 问题，双端设 0 + Arm reboot（无效）。
- 19:0x 双端 mlxfwreset 激活；烟测确认真 DCQCN（标记率 0.06%、CNP 对账），
  定位 RP 旋钮归属 host PF。
- 19:3x–20:3x GBN 轮 7 点。
- 20:4x 双端切 SR（mlxfwreset），SR 轮 7 点。
- 22:0x 双端切回 GBN + USER_PROGRAMMABLE_CC=1（mlxfwreset），
  reboot_recover + pace-shim/RP/agents 重启 + soak cron 恢复，
  E2E 验证通过（pace 49.95G、RP 预算活跃）。
- 之后：解析、出图、summary。

## 追加（2026-07-15）：GBN/SR 验证战役与 lab 停留态变更

用户质疑 GBN↔SR 无差 → 追加验证（详见 summary.md 追加验证节）：
16 纯 RDMA 流 × {neutral, noecn, RED5%} × {GBN, SR 旗标} 共 6 点
（`rdmaonly_*` 目录），加单流深队列探针（RED 4-8MB/2%，profile
`motiv12_srprobe` 留在交换机未删）。判定：SR 旗标行为无效，重传恒为
GBN。**自本日起 lab 默认停留态 = 非 PCC 固件 DCQCN + GBN + 100G 瓶颈
+ neutral ECN（ecn_incast_bzx）+ E 栈全停 + soak cron 注释（#MOTIV#）**，
不再在实验后自动恢复 PCC/HPFT；需要时用 `tools/cc_mode.sh pcc` 拉起
（教程 docs/cc_mode_switching.md）。终态已验证：双端 UPCC=0/SR=0
（current=next），单流 92.53G 健康。

## 追加（2026-07-15 下）：排查期间修复的环境问题

- **VF node GUID 全零** → librdmacm 设备匹配空指针崩溃（rping/
  rdma_client 段错误）。修复：sriov sysfs 写 GUID + VF 驱动 rebind，
  已固化进两台 host 的 vf_setup.sh（按主机名自动编号，原版备份
  vf_setup.sh.bak_guid）。与 SR 无关，纯环境修复。

**lab 终态（2026-07-15 晚终版）**：UPCC=0 + SR flag=0（纯默认固件配置）
+ 100G 瓶颈 + neutral ECN + E 栈全停 + soak cron 注释。**GBN/SR 切换用
ROCE_ACCL 寄存器（cc_mode.sh sr|gbn，秒级、无 fw reset、DCQCN 不动；
寄存器易失，fw reset 后重设；当前=GBN）**。交换机新增 profile：
motiv12_red(108K/396K/5% drop)、motiv12_lowred(20K/60K/5%)、
motiv12_srprobe(4M/8M/2%)，均留存未删。

## 追加（2026-07-15 晚）：ROCE_ACCL 寄存器方法（用户同学提供，验证通过）

`mlxreg -d 38:00.1 --reg_name ROCE_ACCL --set
"selective_repeat_forced_en=0x1"`：秒级强制全部新建 QP 用 SR，普通建连
即可、DCQCN 原样（glacial 探针仍拍到 0.003G）、与 mlxconfig 旗标无关
（flag=0 验证）。SR 指纹：lowred 1.3-1.6 万次丢包、线上字节≈goodput
（GBN 对照多 5.6G 乱序垃圾）。干净 16 流对比 rdmaonly3_*：noecn 下
GBN 32.28G vs SR 91.20G。**这是开 SR 的唯一方案（用户指示）**；
排查期间对 perftest 的临时改动已全部回滚（当前二进制与原版 md5 一致）。

## 追加（2026-07-15 晚）：1.2 SR 半边重测完成

预检（基线/MTU1500/旋钮默认/neutral 绑定）→ GBN 可复现性抽查
（gbn_ecnneutral_repro：0.27G/93.92G/2.5ms，原值 0.34/93.73/3.19，
在 ECN 扫描噪声带内，GBN 七点沿用）→ `cc_mode.sh sr` → 七点重跑
（sr_ecnneutral/aggr/gentle、sr_tcpslow/fast、sr_rdmaslow/fast，全程
无 fw reset）→ `cc_mode.sh gbn` 回默认。旧旗标时代 sr_* 归档到
`_obsolete_flagera_sr/`，CSV 全量重建，两张图重生成。真 SR 与 GBN
逐点无差（SR 轮 seq_err 恒 0 佐证模式确实切换）。lab 终态仍为
基线（寄存器=GBN、neutral、E 栈停）。

## E 栈临时单元重启命令备忘

```
sudo systemd-run --unit hpft-pace-shim --property=Restart=always \
  /usr/bin/python3 /home/zhaoxiang/hyperfront/hpft-v2/tools/host/hpft_pace_shim.py   # sgpu01
ssh hpft-dpu2 'sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py'
ssh hpft-dpu  'sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py'
ssh hpft-dpu  'bash /tmp/rp_service.sh start'
```
