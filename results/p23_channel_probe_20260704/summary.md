# P2-3 通道探测结果(2026-07-04)

## 已验证(端到端)

RP(sgpu01-dpu DPA)--rtt_req/CCMAD 探测--> 接收端 NP(sgpu02-dpu DPA)
--响应负载注入 {w1=rx_rate, w2=cap}--> RP 以 RTT 事件收到并按偏移解析。

- 哨兵值 0x11111111/0x22222222 原样到达 RP(format-3 trace),携带流 flowtag;
- 探测节奏:matched pair 每 epoch(~1ms)由 winner 事件发起;fail-open 流 1/1024 事件;
- 前置:RP 侧 `--remote-sw-handler`(裸旗标);NP=移植的 nic_telemetry 应用
  (`-np-nt`,3.4 树新增构建分支);两卡 PCC_INT_EN=0、UPCC=1。

## 平台结构性约束(判别实验定论)

CCMAD 探测的 SW-NP 分发按**目的 function** 作用域:
- 目的=e-switch owner 域 function(ECPF/SF,如 Arm-to-Arm 流):NP 命中 ✓;
- 目的=host VF:探测在 VF 自身 RX 管线处理,SW-NP 不可达(hits 恒 0,
  且 remote-sw-handler 置位后固件也不再代答 → 探测无响应);
- 与 doca_caps(VF pcc unsupported)一致;NP 也不能建在 VF 上。

## 架构落点

- **VF 流量(主场景)**:接收端驱动通道改走 Arm-to-Arm——接收端 agent
  (OVS 流计数器 per-pair RX + cap 策略)→ UDP → 发送端 agent → RP mailbox
  批量 {pair, cap, rx_rate}。延迟 ~15ms 级,批量突破 75Hz 单条限制。
- **CCMAD NP 通道保留**:对 DPU 域流量即刻可用(µs 级);若未来固件支持
  VF probe steering 可无缝切换。代码资产已入库。

## 排障记录

- packet-handler 上下文调用 doca_pcc_dev_trace_5/flush → DPA 进程 core dump
  (与 RP mailbox 上下文调 nic counters 崩溃同类:**上下文 API 白名单很窄**);
  加固(去 trace、payload 防御性访问)后稳定。
- dpu2 host 树最初未带 S4 的 HPFT stdin/mailbox 补丁 → 从 dpu1 移植。
- 跳板 ssh 偶发 255 且后台 echo 会卡死无读者 FIFO → 一律改 DPU 本地
  service 脚本(start/query/stop,查询带 timeout 防挂死):tools/dpu/。
- 刷机后 Arm SF 网络(原 bf-rdma0)需重配:netdev 现名 enp3s0f0s0,
  representor 需加入 underlay-p0,IP dpu1=10.0.4.201 dpu2=.202(原双 .202 冲突)。
