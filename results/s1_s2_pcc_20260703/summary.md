# S1 + S2 结果:PCC 解锁,PF 与 VF 限速均生效(2026-07-03)

环境:刷机后 DOCA 3.4.0112 / fw 32.49.1014 / flexio 26.04.3229;
PCC 应用构建于 `~/bzx/doca34-apps`(DPU),设备 `mlx5_0`。
RDMA 测试:ib_write_bw(perftest-26015),65536B,5 s,sgpu01→sgpu02。

## S1:参考 RTT 模板算法(未改动)

| 阶段 | vf0 吞吐 |
|---|---|
| PCC 运行前 | 182.07 Gb/s |
| PCC 运行中 | 185.13 Gb/s |
| PCC 退出后 | 182.62 Gb/s |

- doca_pcc Standby→Active 正常,4 端口 user init 完成,SIGINT/超时退出干净。
- 参考算法不损伤流量;多次启停(15 s / 35 s / 145 s 窗口)无残留。

## S2:固定速率算法(rate=52429/2^20 ≈ 5% 线速 ≈ 10G @200G)

补丁:`doca_pcc_dev_user_algo` 恒定返回 `rate=52429, rtt_req=0`,
不区分 algo_slot(绕过 5/28 发现的 slot=0xf internal-algo fallback)。
代码存档:`pcc/s2-fixed-rate/rp_rtt_template_dev_main.c`。

| 流量 | 无 PCC | 固定 10G PCC | PCC 退出后 |
|---|---|---|---|
| PF dpu1→dpu1(101.2.1.x) | 196.03 Gb/s | **9.73 Gb/s** | — |
| **VF0 dpu1vf0→sgpu02** | ~182.6 Gb/s | **9.18 Gb/s** | 182.31 Gb/s |
| VF1 dpu1vf1→sgpu02 | ~183 Gb/s | **9.18 Gb/s** | — |

## 结论

1. **S2 GO:在 DOCA 3.4 + fw 32.49 上,DPA PCC 算法对 host PF 和 host VF 的
   RoCE 流量都能限速**。5/28(旧固件栈)观察到的"VF 不受控"在当前栈上不存在。
   host 侧零改动、零感知(租户透明性成立)。
2. 精度:目标 10.0 Gb/s,PF 实测 9.73(-2.7%),VF 实测 9.18(-8.2%)。
   量级正确;精确标定(含 rate 单位/开销修正)放到 S4。
3. 待查(不阻塞):刷机后 VF 基线从 196 降至 ~183 Gb/s(PF 仍 196),
   疑似 OVS-DOCA 3.4 路径行为变化。

## 下一步(Phase 1 剩余)

- S3:流标识——在算法里 dump flowtag/QPN(`doca_pcc_dev_get_flowtag/get_flow_qpn`),
  4 条并发流(2 src VF × 2 dst)验证可区分性。
- S4:mailbox 动态限速 + 10/100 Hz 更新延迟 + 精度标定。
- S5:per-pair 聚合共享 cap(多 QP/多进程)。
