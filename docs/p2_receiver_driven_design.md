# P2-3 设计:接收端驱动的 cap 与 RX 测量回传(2026-07-04)

## 通道

RP(发送端 DPA)按周期对活跃流置 `results->rtt_req=1` → 固件发 CCMAD RTT
探测包到接收端 → 接收端 **NP 上下文**(`doca_pcc_np_create` + 设备回调
`doca_pcc_dev_np_user_packet_handler(request, response)`)收到探测包:

- `doca_pcc_np_dev_get_raw_packet()` 可解析 IP 头 → **{src_ip, dst_ip} 即
  pair 身份,探测包自带**(dst_vnic≡dst_ip,P2-2 随之解决,无需 host agent
  做 QPN 映射);
- 查本地表 {pair → cap, rx_rate},把 `{magic, cap, rx_rate}` 写入响应负载;

响应回到发送端 → RP 收到 ROCE_RTT 事件 → `doca_pcc_dev_get_rtt_raw_data()`
解析负载 → 更新 pair 的 budget(=cap)与 R 输入(=rx_rate)。

## 接收端 RX 测量(解决 P2-1 残余误差)

接收端 DPU 的 OVS/eSwitch 对每条 megaflow 有**硬件流计数器**(精确字节数,
无事件丢失)。Arm 侧 agent 周期读取 per-{src_ip,dst_ip} 字节计数(
`ovs-appctl dpctl/dump-flows` 或 tc 计数器)→ 算 rx_rate → 连同策略 cap
经 **NP 的 mailbox** 写入 NP DPA 全局表。粒度天然 per-pair。

## 分工总结

| 组件 | 位置 | 职责 |
|---|---|---|
| RP 算法(现 v2 水位)| sgpu01-dpu DPA | rate=min(cc, level);level 控制环的 R 输入改用回传的 rx_rate;周期发 rtt_req |
| NP 回调 | sgpu02-dpu DPA | 解析探测包 IP → 查表 → 响应负载注入 {cap, rx_rate} |
| 接收端 agent | sgpu02-dpu Arm | OVS 流计数器 → rx_rate;策略 cap;mailbox 下发 NP 表 |
| mailbox | 两侧 | 慢速配置与调试(0xdeb) |

## 延迟/频率预算

- cap 变更传播:agent→NP 表(mailbox,~13ms)→ 下一个 RTT 响应(探测周期,
  可 ~1ms)→ RP 生效(µs)。总 ~15ms 内,且 cap 高频变化可直接由 NP 表驱动,
  绕开发送端 mailbox 的 75Hz 上限(每条流每个探测周期都携带最新 cap)。
- rx_rate 更新率 = min(agent 采样率, 探测率)。

## 待验证项

1. RTT 探测的发出率控制(rtt_req 置 1 的节流策略;模板有 rtt_req_to_rtt_sent
   状态机可参考);
2. 响应负载可用字节数(doca_pcc_np_dev_response 结构;switch_telemetry 例子
   为参考——3.4 只保留了该 NP 变体,nic_telemetry 需按其骨架自写);
3. NP 上下文与 RP 上下文能否同进程/同设备共存(sgpu02-dpu 以后也要做发送端);
4. PCC_INT_EN=0(NP 前置条件)在 sgpu02 卡上的状态;
5. OVS 流计数器在 VXLAN/offload 场景下的可见性与稳定性。
