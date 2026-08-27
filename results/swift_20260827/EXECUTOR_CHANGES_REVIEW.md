# 待审：Swift 接入时顺带改动的执行面代码（2026-08-27）

这些改动在 `tools/dpu/pcc/rp_rtt_template_dev_main.c` 里，与 Swift 算法本身无关，改的是 HPFT 执行面的 QP→流对映射，**影响所有 HPFT 实验**。做 Swift 的 agent 在六对同发场景里观察到 qp_count 读数错误后改的；尚未经执行面负责人审阅。motivation 1-2 的 Swift 行已改用独立的原厂模板二进制（`~/bzx/pcc_swift_stock`），不依赖这些改动。审阅人请逐条判断：是真 bug 就留，不是就回退。

| # | 改动 | agent 给出的理由 | 审阅要点 |
|---|---|---|---|
| 1 | QP 映射表的键从 `qpn+1` 改为 `hpft_qkey(qpn, flowtag) = (qpn+1) ^ (flowtag<<8)` | QPN 按每个 VF 各自编号，同一发送端两个 VF 的 QP 撞键：vf3 的事件记到 vf1 的流对，qp_count 读 0–3、速率发错流对 | 生产形态每发送端只用一个 VF 时是否真会撞键；哈希异或是否引入新碰撞；`qpn_resolver` 的 QP 表是否也按同样键 |
| 2 | 只从 `ROCE_TX` 事件学 QP（`learn` 门控），RTT/CNP/NACK 事件不再写映射表 | RTT 事件带的 QPN 每个事件都不一样（84 万事件、85 万次变化），会把每流对 64 个槽位撑满，真 QP 挤不进去 | RTT 事件的 QPN 语义（是探针 QP 还是数据 QP）；CNP/NACK 事件的 QPN 是否可信、丢掉会不会影响 DCQCN 项按 QP 数归一化 |
| 3 | 映射表命中后核对 `g_hpft_pairs[target].flowtag == ft`，不符则按 flowtag 重解析 | 流对删除后重建落到别的下标，映射表仍指旧下标 | 与 `n3_evict_s` 驱逐/重建路径的交互；核对失败时的开销 |

另有一组仅诊断用的计数（`g_hpft_qpn_last/chg/ev_cnt`，`0xdef` 回读），可随审阅一并决定去留。

改动的 diff 在提交 "swift: ..." 里（`git log --grep swift`），与 Swift 代码同一次提交；四台 DPU 上现在跑的就是这个版本。
