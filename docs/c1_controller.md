# C1:统一 controller + registry(2026-07-04)

## 交付

- **registry**(`config/lab-registry.json`):对齐 v1 的 {src_vnic, dst_vnic,
  rate_bps} 接口,加 v2 所需的 representor/flowtag/dpu 字段。version 2。
- **controller**(`tools/hpft-shaper-controller`):单入口,registry 驱动。
  - `--json` 干运行:打印 rate_bps→cap_units 换算计划;
  - `--apply`:渲染 per-dst caps 文件下发接收端 DPU,确保 rx_agent 运行;
  - `--control`:JSON-lines stdin 运行时改速
    `{"cmd":"set","src_vnic":..,"dst_vnic":..,"rate_bps":..}`。

## 端到端验证

registry vf0 规则经 JSON-lines set 6G → controller 渲染 cap_units 31457 →
接收端 caps 文件 → rx_agent → 发送端 tx_agent2 → RP pair → **vf0 wire 6.06G**。

## 组件拓扑(分布式必然,由 controller 统一驱动)

| 组件 | 位置 | 角色 |
|---|---|---|
| hpft-shaper-controller | 任意(操作入口) | registry 解析、下发、运行时控制 |
| RP (doca_pcc) | 发送端 DPU DPA | per-QP 硬件 pacing + 水位/CC |
| tx_agent2 | 发送端 DPU Arm | 本地 vport R + flowtag mailbox |
| rx_agent | 接收端 DPU Arm | per-dst representor 测量 + cap 策略 |
| qpn_resolver + mdst_controller | 多 dst 时 | qpn→dst 解析 + 显式 pair 下发 |

## 运维记录(重要)

- 需要 SSH 的进程(qpn_resolver)必须在登录用户环境跑;systemd 裸环境无
  HOME/.ssh → ssh 失败。tx_agent2/rx_agent(仅 ethtool+UDP+FIFO)可 systemd,
  但转瞬 bind 冲突需 SO_REUSEADDR(已加)+ reset-failed;当前用 keepalive
  wrapper(while-loop)最稳。生产化时统一为带正确 Environment 的 systemd unit。
- 全部常驻进程清单:发送端 DPU {doca_pcc(rp_service), tx_agent2(keepalive)};
  接收端 DPU {rx_agent(systemd)};host {qpn_resolver 仅多 dst}。
