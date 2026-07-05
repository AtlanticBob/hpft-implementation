# TCP shaper 降速响应性能(2026-07-05,含方法学修正)

问题:TCP shaper 高频速率变动性能是否够好、频率能到多高。

## 方法学修正(关键)

第一版测试有三个错误,导致把"~200ms 延迟、~5Hz 上限"的错误结论:
1. **测了升速**(2G→8G)。升速的收敛是 TCP cwnd 慢启动/爬升,是拥塞控制行为,
   **与 shaper 无关**。测 shaper 的速率变动延迟只能用**降速**——降速时 shaper
   要立即压低发送速率(EDT 改时间戳 + fq 立即按新速率 pace),不依赖 cwnd。
2. **用 CLI 工具改速**。`tcp-shaper-controller` 每次调用要 fork python + parse
   registry + open bpf maps,单次开销 **136 ms**,完全淹没了真实响应。正确做法
   是常驻进程 + direct bpf() syscall(不 fork)。
3. **粗采样**。10 ms 采样 + 15 ms 滑动窗口本身引入 ~15 ms 检测延迟。

修正后:只降速、in-process direct bpf writer、2 ms 采样 + 4 ms 窗口 + 轨迹分析。

## 修正后的真实数据

**控制路径**(direct bpf() map 写入,不 fork):

| 方式 | 每次更新 |
|---|---|
| in-process DirectBpfMapWriter | 48–180 µs |
| CLI 工具(fork+init) | 136 ms ← 不可用于高频 |

**数据面降速响应**(15G→5G,20 次重复,in-process 改速):

| 度量 | 值 |
|---|---|
| settle 到 ±12%(15 ms 窗口判据) | 22.8 ms(p50),含 ~15 ms 窗口假象 |
| **真实拐点(4 ms 窗口,cross ≤6G)** | **6.9 ms(p50),min 6.7 / max 8.4** |

降速轨迹(单步,ms since cmd → Gbps @4ms 窗口):
```
 4.0ms 10.4G → 5.1ms 7.1G → 6.2ms 5.2G(到位并稳定)
```
从 15G 降到 5G 的过渡区仅约 2 ms;bpf 写入 180 µs;总真实响应 **6–7 ms**。

**连续阶梯降**(15→13→11→9→7→5→3G,每步 dwell 仅 30 ms,in-process):

| cap | 实测 | 误差 |
|---|---|---|
| 15G | 15.7 | +4% |
| 13G | 13.6 | +5% |
| 11G | 11.5 | +4% |
| 9G | 9.4 | +4% |
| 7G | 7.3 | +5% |
| 5G | 5.2 | +4% |
| 3G | 3.1 | +4% |

每步 30 ms > 6–7 ms 收敛,全部干净降到位(+4~5% 是 line-rate/goodput 口径 +
burst,非跟随误差)。

## 结论(修正)

1. **TCP shaper 降速响应 ≈ 6–7 ms**(单次,极一致)。控制路径 48–180 µs。
2. **能支持的独立降速频率 ≈ 140 Hz**(1000/7 ms);实测 30 ms/步阶梯干净跟随。
3. **前提是用常驻控制器 + direct bpf(不 fork)**;CLI 工具每次 136 ms fork
   开销是第一版错误结论的主因。
4. **升速慢(~200 ms)是 TCP cwnd 行为,不是 shaper**;测 shaper 只看降速。
5. 对比 RDMA:RDMA 控制路径 mailbox 13 ms(75 Hz),数据面硬件 pacing 快;
   TCP 控制路径 48 µs、数据面降速 6–7 ms——**TCP shaper 的降速敏捷度实际优于
   RDMA 的 mailbox 控制路径**。之前"TCP 不如 RDMA 敏捷"的结论是测量假象。

## 工程要点

生产控制面必须是**常驻进程 + direct bpf() map 写入**(像 RDMA 侧的 tx_agent /
controller),绝不能每次 fork CLI 工具。这一条同时是把 TCP 纳入统一 controller
时的硬性要求。
