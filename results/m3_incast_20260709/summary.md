# M3 — incast / root 层短缺分摊（方案 E，design_e §5.3）

日期：2026-07-09（UTC）。接收端 p1 降速 200G→100G（ethtool，已验证方法），
4 个 VF 对 RDMA 等权打满（每流单独可跑 ~185G，聚合需求 ≫ 100G 下行 =
incast 在最后一跳物化）。rx_agent 新增**下行线速动态感知**（sysfs ~1Hz →
C_root 跟随，日志确认 97.0G）。

## 验收结论

| 标准 | 结果 |
|---|---|
| 各流集合 = 权重份额 ±10% | **通过**：Run A 四流 21.90/21.89/21.89/21.89G（spread **0.05%**，各 −4.5% 于 24.25G 份额）；churn 后再收敛 23.96/23.84/23.84/23.84（spread <0.5%） |
| headroom 生效（队列证据低于无 headroom 对照） | **机制验证通过，保护性对照不可测**（见下） |

## 三组实验

- **Run A（headroom 3%，C_root=97G）**：四流各 21.9G goodput，瓶颈路径
  ping 1000 样本 avg 0.037ms / max 1.06ms / 0 丢包——**零物理排队**。
- **Run B（headroom 0，C_root=100G）**：四流各 22.2-22.5G（分配上移 ✓
  账本正确跟随），ping 与 Run A 完全相同（avg 0.037ms，0 丢包）。
  **诚实结论**：当前参数下两条 AIMD 律的锯齿使聚合稳态欠 ~5%，无论
  headroom 取值聚合都不顶线速 → 物理队列两者皆零，对照差异低于测量地板。
  headroom 的保护价值在更激进参数（大 A）或高流数聚合抖动下才显现——
  移入 M4 参数扫描量化。机制本身（C_root=(1−h)×线速、分配缩放）已验证。
- **Run C（churn：3 流稳态 + 第 4 流乐观起步加入）**：加入前三流各 30.8G
  （97/3 等分 −4.6%）；vf3 以 Tree=50G 突发加入 → 瞬时聚合顶穿线速，
  ping **个别样本 max 114ms**（p99 仍 0.05ms）= Q20 乐观起步的物理瞬态
  代价，~100ms 内被标记压制吸收；在位流被压去让位（30.8→23），**~2-3s
  （50 周期）后四流等分收敛**。root 层短缺随权重自动摊派，无任何显式协议。

## 平台/实现注记

- p1 降速期间 in-band 遥测（同走 p1）随链路闪断 ~10s，agent fail-open
  行为正确（frozen→恢复）；实验均在链路稳定后进行。
- systemd-run 临时单元 stop 后偶发 "Unit already exists"，需
  `systemctl reset-failed` 后再拉起（已遇一次，作废重跑）。
- 数据：`rx_e_A/B/C.jsonl`（逐 tick 全量）、`m3a/b/c_*.log|txt`。
- lab 已恢复：p1=200G、headroom=3%、vf0 MaxRate=6G。
