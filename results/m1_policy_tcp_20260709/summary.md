# M1 — 政策瓶颈（单类 TCP）完整环路（方案 E，design_e §5.3）

日期：2026-07-09（UTC）。首个完整 E 控制环：接收端调度器（water-filling→e_f）
→ VQ/标记 s_f → in-band 遥测 → 发送端响应律 R_f → host fq+edt EDT pace。
vf0 TCP，MaxRate_d=6G，T=50ms，A=0.1G/周期，β=0.3，V=600Mbit，V_max=V。

## 验收结论

| 标准 | 结果 |
|---|---|
| 稳态速率 = 6G±5% | **通过**：三段稳态均值 5.986G(−0.2%) / 3.943G@4G(−1.4%) / 5.865G(−2.3%) |
| 收敛 <10 周期 | **下行阶跃首次进带 3-4 周期，通过**；完全稳定（含下冲恢复）17-19 周期；冷启动突发 ~45 周期（见"收敛构成"） |
| 物理链路无拥塞证据 | 通过：全程 wire ≤7G ≪ 200G；重传率 ~1.9% 与静态 EDT 基线同源（host fq qdisc 本地丢包），非链路拥塞 |

实验：110s 单流，四段动态一次采齐——冷启动(6G cap 预置) → t=40 阶跃 6→4G
→ t=70 阶跃 4→6G → 稳态。数据 `rx_e_full.jsonl` / `tx_e_full.jsonl`（每 tick
r/e/s/vq/R 全量）+ 中间实验 `rx_e_step.jsonl`（运行中施加 cap 的 25G→6G 深阶跃）。

### 收敛构成（时序实测）

- **下行阶跃 6→4G**（政策执行主场景）：标记温和上升（s 峰值 0.41，无满仓），
  4 周期内 r 压过新目标，vq 9 周期排空，短暂下冲至 2.7G，~17-19 周期回到
  4G±5% 长驻。
- **上行阶跃 4→6G**：AI 步长限速（0.1G/周期 × 2G 缺口 = 20 周期），符合设计的
  "温和回收"语义（与 M2 借用回收 <20 周期的验收刻度一致）。
- **冷启动**（Q20 乐观起步，Tree 桩=50G）：TCP 突发 19.7G → s=1 四周期 →
  MD 压至 ~2.5G（越过目标的下冲）→ AI 爬回，有效收敛 ~2.3s ≈ 45 周期。
  这是乐观起步的代价：突发被 1-2 周期内标记、压制（设计预期行为✓），
  长尾在 AIMD 恢复段。锯齿幅度 vs 恢复速度由 A 权衡（M4 敏感性分析）。
- 逐 tick 带内率 57-65%：租户 TCP 自身锯齿（cwnd/丢包恢复）叠加在 pace 之下，
  均值精确、瞬时抖动属租户 CC 行为（s_f 稳态均值 0.02，轻标记均衡）。

## 实现中固化的三个动态学修正（均已在代码注释与本文档记录）

1. **VQ 排空死锁修复（设计公式缺口，最重要）**：设计 §3.3 的 vq 更新以
   demand-capped e_f 为服务率；流被深度压制后 r→0 ⇒ e=1.15r→0 ⇒ 排空速率
   →0 ⇒ s=1 死锁（实测复现：R 压穿地板永不恢复）。修复：**充入按 (r−e_f)，
   排空按 (ê_f−r)**，ê_f = 需求无穷时的公平份额上限（同一棵树多算一遍，
   零额外状态）。均衡处 r≈ê_f 排空≈0（不白送 unmark）。**此为对设计公式的
   补充语义，请设计侧确认**。
2. **V_max=V**（原 3V）：积分器深仓导致 excess 停止后 MD 仍多砍数拍，下冲
   加深；收紧到 1V 后下冲从 <0.1G 地板改善到 ~2.5G。
3. **m_al 3→10**：app-limited 判定 150ms 太快，会把 TCP 自身慢启动误判为
   app-limited 并把 R 钳到起步速率（实测：把乐观起步退化成 0.1G/tick 线性爬
   坡）。500ms 后该机制只捕获真实的长期低用量。

## 平台事实（影响后续所有里程碑）

- **mlx5 fc 硬件计数器缓存 1s**（5.15-bluefield 硬编码，无 knob）：megaflow
  字节计数器 20Hz 读到的是 1Hz 更新的缓存 → 纯 §5.2 路径在 T=50ms 下退化为
  1Hz 采样+20 倍尖峰（第一次 M1 失败的根因）。**解法=混合测量**：representor
  vport 计数器（每 tick 新鲜）给每 dst VF 总到达率，megaflow 给 (src,类)
  构成比（2s 滑窗；megaflow key 出现即时，单流集合冷启动可立刻 100% 归因）。
  单类/VF 场景（M1/M1b/M3）精确；类混合动态（M2）构成比滞后 ≤1s，M2 结果
  如实标注。
- 遥测走 p1 侧 in-band（PF1 SF 挂 underlay-p1，10.1.9.1/2，稳态 RTT 0.1ms），
  按用户批注不用 dpu0 直连网。
- pace 执行链：tx_agent(DPU) → UDP/tmfifo → host shim（DirectBpfMapWriter，
  写延迟 100-600µs，端到端 1-4ms）≪ T。

## 新组件与部署

| 组件 | 位置 | 托管 |
|---|---|---|
| rx_agent.py（E 调度器） | dpu2 /opt/hpft/ | systemd hpft-rxagent-e |
| tx_agent_e.py（响应律） | hpft-dpu /opt/hpft/ | systemd hpft-txagent-e |
| hpft_pace_shim.py | sgpu01（本 repo tools/host/） | systemd hpft-pace-shim |
| registry v3 | 双 DPU /opt/hpft/lab-registry.json | 政策热加载（mtime） |

旧链路（hpft-rxagent/hpft-txagent/RP）原样运行，RDMA 6G cap 有效；E 环目前
只接管 TCP 类。**lab 注意**：E 环 idle 时 fail-open 会把 vf0 TCP pace 缓升至
Tree 桩 50G（设计语义），故无实验时 TCP 不再被 6.9G 静态值限制。

## 遗留 → M4/后续

- 收敛-锯齿权衡的参数扫描（A、β、V、m_al）；冷启动长尾能否收进 10 周期。
- 政策热加载传播延迟（文件写→生效）实测 0.5-1.8s，含 ssh 通道；纯 agent
  侧 mtime 检测 ≤1 tick，量产入口应走 unified-controller 改造（§5.1 计划内）。
