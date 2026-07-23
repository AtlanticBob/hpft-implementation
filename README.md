# HPFT — implementation repo

HPFT 是 DPU 边缘虚拟队列公平系统：在 BlueField-3 DPU 上用虚拟队列的
差分标记实现云租户间/租户内 TCP-RDMA 类间的两层公平，租户透明、零数据面
改动。这个仓库是**实现**——DPU/host agent 代码、lab 配置、工程验证数据。

**2026-07-23 起项目拆成三个仓库**，各管一摊：

| 仓库 | 内容 |
|---|---|
| **hpft-v2**（本仓库） | 实现：`tools/`、`pcc/`、`tcp/`、`config/`，工程验证/回归数据（`results/`） |
| [`hpft-design`](../hpft-design) | 设计文档：`docs/`，含系统设计的权威说明和历史决策记录 |
| [`hpft-paper`](../hpft-paper) | 论文材料：草稿、motivation 实验包、论文评估章节引用的实验数据 |

三个仓库都是从这个仓库的 git 历史用 `git filter-repo` 切出来的，各自保留了
对应路径的完整提交历史（作者、日期、commit message 都在）。**约定放在同一个
父目录下作兄弟目录**（`~/hyperfront/hpft-v2`、`~/hyperfront/hpft-design`、
`~/hyperfront/hpft-paper`）——本仓库大量脚本用绝对路径引用同级的
`~/hyperfront/perftest-26015`、`~/hyperfront/bfb` 等外部依赖，挪动父目录
结构会破坏这些引用。

## 先读什么

1. **系统设计、机制、为什么这样设计**：去 `hpft-design` 仓库的
   `docs/design_and_implementation.md`——这是唯一权威的设计文档，从头
   讲清楚系统的样子、每个部件解决什么问题、以及为什么。
2. **这个仓库怎么用**：往下看"现行系统"和"代码地图"两节。
3. **动 lab 前必读**：`docs/ops_notes.md`（在 `hpft-design` 仓库）——
   平台缺陷病历与实验卫生规则，每一条都有事故背书。
4. **实验数据找什么、信不信得过**：`results/EXPERIMENT_STATUS.md`——
   哪些实验是旧配置（2026-07-13/14 三个配置边界之前），结论按边界打折。

## 现行系统

三个服务，systemd 托管，配置来自 `config/lab-registry.json`（**repo
副本是唯一真值**，改政策先改 repo 再下发到两台 DPU 的 `/opt/hpft/`，
接收端 mtime 热加载 policy 段）：

- 接收端 DPU（`ssh hpft-dpu2`，经 sgpu02 跳板）跑 `hpft-rxagent-e`
  （`tools/dpu/rx_agent.py`）：vport 硬件计数器直读 → 三层 water-filling
  → 虚拟队列标记 → 带内遥测。
- 发送端 DPU（`ssh hpft-dpu`）跑 `hpft-txagent-e`
  （`tools/dpu/tx_agent_e.py`）：MIMD 响应律（乘性增硬顶 ê + 乘性减，
  MD 另有对称硬顶下限）→ 发送端树 → 执行（RDMA 走 PCC mailbox，TCP
  经 UDP 发给 sgpu01 上的 `hpft-pace-shim` 写 BPF map）。
- RDMA 侧执行面是 `tools/dpu/pcc/rp_rtt_template_dev_main.c`（DOCA PCC
  device 代码，跑在 DPA 上），`rate = min(cc_rate, level)`；`cc_rate`
  是 PCC 里自实现的 DCQCN 风格状态机（独立于 tx_agent 的 MIMD）。

控制周期 1ms（`config/lab-registry.json` 的 `period_ms`）。生产参数：
`mi_alpha=0.6`、`beta=0.15`、`v_periods=2`——具体数值和为什么这样调，
见 `hpft-design` 仓库的 `docs/response_law_mimd_analysis.md` 和本仓库
`results/convergence_opt_20260722/summary.md`。

## 硬性规则

- PCC device 码（`tools/dpu/pcc/`）改动前先备份（`backup/` 惯例）；改
  完要 `meson setup --reconfigure build && ninja -C build pcc/doca_pcc`
  （在 hpft-dpu 上）才会真正生效，改完不重编是常见坑。
- TCP shaper 叫 "host fq+edt"，不叫 "opt3"；DPU 侧 TCP 卸载（原 T3.2）
  已暂停（架构性负结论，见 `hpft-design` 仓库 `docs/archive/
  superseded-designs.md`），除非用户重提不要重启。
- dpu2 underlay-p1 上的分类 OpenFlow 规则、遥测通道等运行时状态，
  重启会丢，`rx_agent`/`cc_mode.sh` 的恢复流程会自动重装，不要手工删。
- lab 默认停留态、切换 CC 模式：`tools/cc_mode.sh status`/`dcqcn`/`pcc`
  （用法见脚本头注释）。

## 代码地图

- `tools/dpu/rx_agent.py`、`tools/dpu/tx_agent_e.py` —— 现行两端 agent；
  `tools/dpu/fastfill.py` —— 单遍 fair-share 上限算法（两端共用）；
  `tools/dpu/vport_meter.c` —— 独立于 agent 之外的 1ms vport 计数器
  采样常驻进程（`hpft-vport-meter` systemd unit）。
- `tools/dpu/pcc/` —— DOCA PCC device 代码（RDMA 执行面，跑在 DPA 上）。
- `tools/host/hpft_pace_shim.py` —— host 侧 BPF 写入桥（sgpu01）。
- `tools/lab-infra/vf_setup.sh` —— VF 重建脚本（`cc_mode.sh` 的
  `post_recover` 调用）。
- `tools/cc_mode.sh` —— lab CC 模式切换（DCQCN ↔ PCC+HPFT，GBN ↔ SR）。
- `tcp/bpf-opt3/` —— TCP 执行面（host fq+edt 的 BPF 实现）。
- `config/lab-registry.json` —— 唯一真值配置。
- `results/<experiment>_<UTC日期>/` —— 工程验证/回归实验产物；论文
  评估章节引用的那部分实验已经搬到 `hpft-paper` 仓库，此处剩下的是
  纯工程向的验证/压测数据，见 `results/EXPERIMENT_STATUS.md` 的分类。
