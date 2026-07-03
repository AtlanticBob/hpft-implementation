# S3 结果:流标识(2026-07-03)

探测方式:固定 10G 算法 + per-flow 首事件 `doca_pcc_dev_trace_5(1, flowtag,
qpn, port, ev_type, ev_subtype)`。代码见 DPU `~/bzx/doca34-apps`(S3 patch)。
注意:trace 打印模板标签错位,按参数顺序解读(algo slot 位=flowtag,
result rate 位=qpn,result rtt req 位=port)。

## Run 1:五条已知流(错峰)

| 流 | flowtag | DPA qpn | perftest 本端 QPN | port | 吞吐 |
|---|---|---|---|---|---|
| F1 vf0→10.1.0.2 | 0x74249a41 | 0x1e0 | 0x01e0 ✓ | 1 | 9.18G |
| F2 vf1→10.1.1.2 | 0x11f4386b | 0x15b | 0x015b ✓ | 1 | 9.18G |
| F3 PF dpu0→101.2.0.2 | 0x24cb08a6 | 0x1c1 | 0x01c1 ✓ | 0 | 9.73G |
| F4 PF dpu1→101.2.1.2 | 0x0d228ea8 | 0x2c3 | 0x02c3 ✓ | 1 | 9.73G |
| F5 vf0 2QP(magic 守卫 bug 未 trace) | — | — | 0x1e1/0x1e2 | — | 18.36G |

## Run 2:vf0 1QP + vf0 2QP(修正守卫后)

- 三个 QP(0x1e3/0x1e4/0x1e5)**flowtag 全部 = 0x74249a41**,与 Run 1 的 vf0
  一致(跨运行、跨进程、跨 QP 稳定)。
- (ft,qpn) 守卫在共享上下文中反复翻转 → 每事件都 trace(~15 万条/QP,595MB
  日志,已清理;去重计数存 `G_run2_uniq_flowtag_qpn.txt`)。
- 吞吐:1QP=9.18G,2QP 进程=18.36G(每 QP 都被独立钳到 ~9.18G)。

## 结论(对 v2 设计的直接输入)

1. **src_vnic 映射:flowtag 即源 function 标识**,稳定且事件内直接可得。
   构建 {flowtag → src_vnic} 表即可(每 VF 一条,控制面启动时校准)。
2. **flow_qpn = 发送端本地 QPN**,与 host 侧可观测的 QPN 空间直接对应。
3. **算法上下文(algo_ctxt)按 flowtag 共享,非 per-QP**;而 `results->rate`
   按触发事件的 QP 生效(2QP 各 9.18G 证实)。⇒ v2 的 per-QP/per-pair 状态
   必须自建于 DPA 全局内存(哈希表),不能依赖 algo_ctxt。
4. **dst_vnic 不在事件字段中**。方案:Arm/host agent 维护 {(flowtag|src_vnic,
   qpn) → dst_vnic} 映射(host 侧 `rdma res show qp`/cm_id 可见本地 QPN 与对端,
   属 provider 域,不触碰租户),经 mailbox 下发 DPA;S4/S5 一并验证。
5. 教训:生产算法严禁 per-event trace(595MB/30s);probe 亦须全局去重。

## 附带发现

- PF 流量(9.73G)与 VF 流量(9.18G)在同一固定 rate 下有 ~6% 达成差,
  疑与路径开销相关,S4 标定时一并处理。
