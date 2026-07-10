# HPFT Shaper v2 — 项目入口（2026-07-10 全面更新）

这是新会话/新接手者的唯一入口。项目的当前形态：**方案 E（边缘虚拟队列
政策标记 + 统一响应律）已实现完成并成为实验室的现行控制面**，在既有的
RDMA PCC shaper 与 TCP host fq+edt 之上实现了租户间/类间两层公平，
覆盖 TCP 与 lossy RDMA。里程碑 M0-M5 全部验收通过，参数已终裁，目前
处于总结归纳阶段（论文写作待用户启动）。

## 阅读顺序

1. 本文件；
2. 自动 memory（每会话自动加载）：`eurosys-rd-fairness-design`（设计与
   实现全程）、`hpft-v2-status`（lab 事实）、
   `writing-style-for-research-docs`（写作规范，必须遵守）；
3. `docs/rd_fairness_design_e.md` **v2.2** —— 设计正文（术语表、机制、
   Q1-Q27 决定日志、参数终裁与三条实测权衡）；
4. `docs/evaluation_index.md` —— 全部实验数据的总索引；
5. `docs/ops_notes.md` —— 平台缺陷病历与实验卫生规则（动 lab 前必读）；
6. `docs/standing_deficit_analysis.md` —— 利用率缺口的诊断记录。

## 现行系统（E 栈三件套）

接收端 DPU（`ssh hpft-dpu2`，经 sgpu02 跳板）跑 `hpft-rxagent-e`
（`/opt/hpft/rx_agent.py`）：逐流集合混合测量 → 三层 water-filling →
虚拟队列标记 → 带内遥测。发送端 DPU（`ssh hpft-dpu`）跑 `hpft-txagent-e`
（`/opt/hpft/tx_agent_e.py`）：统一响应律（含三段式恢复）→ 发送端树 →
执行（RDMA 走 PCC mailbox，TCP 经 UDP 发给 sgpu01 上的 `hpft-pace-shim`
写 BPF map）。三个服务都是 systemd 托管，配置来自
`config/lab-registry.json`（**repo 副本是唯一真值**，改政策先改 repo
再 scp 到两台 DPU 的 /opt/hpft/，接收端 mtime 热加载）。

旧链路（hpft-rxagent / hpft-txagent，即 tx_agent2 时代）已停役未卸载，
其缺陷病历见 ops_notes；**不要**把它们拉回来与 E 栈并跑（会争抢 PCC
mailbox）。

## 硬性规则（更新版）

- PCC device 码（`pcc/device/`）不动，确需动先问用户；
- devlink port function rate 已解禁（fw 32.49.1014 复测干净，见
  ops_notes），生产依赖前仍需浸泡；
- dpu2 underlay-p1 上的三条分类 OpenFlow 规则是运行时状态，rx_agent
  启动时幂等自装，不要手工删；
- TCP shaper 叫 "host fq+edt"，不叫 opt3；T3.2（TCP DPU 卸载）保持
  暂停，除非用户重提；
- 实验卫生四条（registry 真值 / 实验前停 soak 流量 cron / 批次间重启
  RP / pkill 与目标启动分开 ssh）见 ops_notes，**每条都有事故背书**；
- 实验环境扰动操作已预授权，无需逐次询问。

## 代码地图

- `tools/dpu/rx_agent.py`、`tools/dpu/tx_agent_e.py` —— E 栈两端；
  `tools/dpu/fastfill.py` —— 单遍 ceiling 算法（两端共用）；
- `tools/host/hpft_pace_shim.py` —— host 侧 BPF 写入桥（sgpu01，
  systemd hpft-pace-shim）；
- `tools/soak/` —— 长稳浸泡（cron：流量轮换 */15 + 看门狗 */5，
  结果在 `results/soak_20260710/`）；
- `tools/dpu/rp_service.sh` —— RP 运维（start 已带 sleep-infinity
  FIFO 保活）；
- `pcc/`、`tcp/bpf-opt3/` —— 复用的既有执行面，本轮未改动；
- `results/<probe>_<UTC日期>/` —— 实验产物，索引见 evaluation_index.md。

## 挂起的决策（属用户）

1. RP 内部状态劣化的根治需要进入 device 码排查（禁区），临时对策是
   批次间重启 RP；
2. D-ablation（租约版对照）用户明确暂缓；
3. per-class A 与份额保底两个具名配置模式是否写进论文正文；
4. 论文写作的启动时机。

## 历史文档

`HANDOFF_IMPL_E.md` 是实现启动时的交接（2026-07-09），其任务已完成，
仅作历史参考；其中的"已知 bug"（tx_agent2 卡死）已根治并有完整病历。
T3.2 与更早的 RDMA/TCP shaper 报告见 `docs/` 与 `results/` 对应目录。
