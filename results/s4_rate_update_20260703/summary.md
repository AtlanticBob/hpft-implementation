# S4 结果:mailbox 动态限速(2026-07-03)

机制:device 侧 `doca_pcc_dev_user_mailbox_handle` 写 volatile 全局速率,
算法每事件读取;host(Arm)侧 `HPFT_RATE_STDIN=1` 环境变量启用 stdin/FIFO
驱动循环,每次 `doca_pcc_mailbox_send`(4B 请求)并计时。
测量:host 侧 5ms 采样 `port_xmit_data`(线上口径);DPU/host 钟差 ~88226s
(刷机后无 NTP)已用 SSH 往返校准(±0.15s)。

## P1 精度扫描(每级 8s,取 2–7.5s 窗口)

| 目标 | 达成 | 误差 |
|---|---|---|
| 1G | 0.97 | -3.0% |
| 2G | 1.94 | -3.2% |
| 4G | 3.87 | -3.1% |
| 8G | 7.76 | -3.0% |
| 12G | 11.79 | -1.7% |
| 16G | 15.51 | -3.0% |
| 32G | 31.13 | -2.7% |
| 64G | 62.49 | -2.4% |

系统性 ~-3%(线头开销口径差),**验收门(≤5%)通过**。goodput 口径会再低
~5%(65536B 消息的协议头占比),标定时按口径换算即可。

## P2 活跃流跟踪 10 Hz 变速(8G/4G 方波 20s)

- 驱动侧达成 10.03 Hz;吞吐双峰 3.92G / 7.71G(期望线上 3.88/7.76),
  p5=3.87、p95=7.76,mean 5.81G——**活跃流精确跟踪,无粘滞**。

## P3 更新通道极限

- 单次 `doca_pcc_mailbox_send`(同步 RPC,DPA 回调完成后返回):
  首测含 trace_flush 12.7ms;去掉 handler 内 trace 后仍 p50≈13.2ms
  (min 13.17 / p99 14.72 / max 15.25)→ 该延迟是 mailbox RPC 本身的开销。
- 背靠背 200 次:持续 **75.3 Hz**。
- 生效延迟上界 = RPC 完成时间 ≈ 13ms(回调在返回前已执行,实际生效更早)。

## 验收对照

| 目标 | 结果 |
|---|---|
| 100 Hz 稳定 | ✗ 单 RPC 通道 75 Hz 封顶 → **Phase 2:批量更新**(一次 RPC 携带 N 个 {pair,cap};request buffer 可扩大) |
| 生效延迟 ≤10ms | 边缘(≤13ms 上界;实际生效早于 RPC 返回,待更精细测量) |
| 误差 ≤5% | ✓(线上口径 ≤3.2%) |
| 10 Hz | ✓(10.03 Hz,双峰干净) |

## 数据文件

- `driver_log.csv`(DPU 侧指令时间戳)、`bw_samples.csv`(host 5ms 采样)、
  `pcc_sends.log`(每次 send 的 t0/耗时)、`flow_client.log`。
