# RDMA 降速响应优化:430ms → ~5-7ms(2026-07-05)

用户目标:RDMA shaper 降速响应太慢(430ms),优化。假设:PCC 对 VF 的快速控制
信道不可达(仅 SF 可达),用 SF 代理 VF。

## 根因(DPA 内窥坐实,非之前所想的投递/硬件)

用 `0xdeb <idx>` 查询实时读 DPA 的 `{budget, level, remote_rx_rate}`:
- **不是 DPA pacing 慢**(pacing 即时),也**不是 mailbox 慢**(实测 sub-ms)。
- 是**接收端驱动的积分控制环收敛慢**:
  1. level 是 per-QP 速率,水位控制收敛到 bud/N;
  2. cap 变化时旧代码 `level=ebud`(整个预算=N=1 的值),对 N QP 过高 N 倍;
  3. 小步积分每步只动 ±12.5%、且被 ~20Hz 的新鲜 R 采样门控;
  4. tx_agent 用 `ethtool -S`(每 dev fork ~15ms,4 dev=~60ms/tick)把采样卡在 ~16-20Hz。
  15G→5G 降到 1/3:0.875ⁿ=0.333 → n≈8 步 × 50ms ≈ 400ms,精确匹配。

## 三处修复(全在软件层,无需 SF NP 代理)

- **B 比例前馈**(设备码):cap 变化时 `level ← level × new_bud/old_bud`。level 本
  收敛在 old_bud/N,乘比例一步落到 new_bud/N(与 N 无关)。0xdeb 实测:level 在
  投递瞬间一步跳到 bud/N(4QP@5G:1.30G,目标 1.25),不再慢爬。
- **settle-hold**(设备码):cap 变化后冻结积分 3 个新鲜-R 步(~60ms),让 wire
  先跟上前馈 level,再恢复积分微调。消除了"积分对 stale-high R 过度反应→level
  砸到底→振荡"导致的双峰不稳(修复前偶发 744ms 离群)。
- **A faster agents**(tx/rx_agent):`ethtool -S` → sysfs `statistics/{rx,tx}_bytes`
  (速率完全一致,已验证),免 fork;HZ tx 20→50、rx 20→100。投递+R 刷新提速 ~3×。

## 结果

| 度量 | 修复前 | 修复后 |
|---|---|---|
| 降速 command→wire settle(生产路径,6 次)| 430ms | **7-9ms,中位 7ms** |
| 单时钟直接注入 DPA→wire(6 次)| — | **5-6ms** |
| 一致性 | 偶发 744ms 离群 | **无离群** |
| M1 cap 精度(8G)| 7.34G | 7.31G |
| M2 空闲延迟 typ | 2.77µs | 2.52µs |
| M3 饱和延迟 typ | 2.79µs | 2.22µs |
| M4 饱和吞吐 | 7.34G | 7.31G |
| 4-QP 公平 spread | 0.00G | 0.00G |

**RDMA 降速 430ms → ~5-7ms(~60-85×),与 TCP 6-7ms 持平,零回归。**

## 关于 SF 代理

用户提的 SF NP 代理**最终未采用**——因为诊断发现瓶颈是控制律 + agent 采样率,
不是 mailbox/投递信道(mailbox 实测 sub-ms,并非之前误记的 13ms)。都在软件层
修好,无需新建 NP 基础设施。方向直觉正确(要更快的控制),实际解法更简单。
若未来 mailbox 成为瓶颈(如需 >100Hz 连续改速),SF NP 代理仍是 P2-3 备好的路。

## 复现/运维

- 设备码改后必须 `rm -rf build && meson setup build -Denable_all_applications=false
  -Denable_pcc=true && ninja -C build`(device 在 meson-setup 阶段 dpacc 编译);
  再 `bash /tmp/rp_service.sh start` 重启 RP(tx_agent ~50ms 内重灌 caps)。
- 观测:`echo "0xdeb <pair_idx>" > /tmp/rp_fifo`;RP 日志 `HPFT_RSP bud/lvl/r`。
