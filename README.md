# HPFT — implementation repo

HPFT 是 DPU 边缘虚拟队列公平系统：在 BlueField-3 DPU 上用虚拟队列的
差分标记实现云租户间/租户内 TCP-RDMA 类间的两层公平，租户透明、零数据面
改动。这个仓库是**实现**——DPU/host agent 代码、lab 配置、工程验证数据。

**2026-07-23 起项目拆成三个仓库**，各管一摊：

| 仓库 | 内容 |
|---|---|
| **hpft-implementation**（本仓库） | 实现：`tools/`、`pcc/`、`tcp/`、`config/`，工程验证/回归数据（`results/`） |
| [`hpft-design`](../hpft-design) | 设计文档：`docs/`，含系统设计的权威说明和历史决策记录 |
| [`hpft-paper`](../hpft-paper) | 论文材料：草稿、motivation 实验包、论文评估章节引用的实验数据 |

三个仓库都是从这个仓库的 git 历史用 `git filter-repo` 切出来的，各自保留了
对应路径的完整提交历史（作者、日期、commit message 都在）。**约定放在同一个
父目录下作兄弟目录**（`~/hyperfront/hpft-implementation`、`~/hyperfront/hpft-design`、
`~/hyperfront/hpft-paper`）——本仓库大量脚本用绝对路径引用同级的
`~/hyperfront/perftest-26015`、`~/hyperfront/bfb` 等外部依赖，挪动父目录
结构会破坏这些引用。

## 先读什么

1. **系统设计、机制、为什么这样设计**：去 `hpft-design` 仓库的
   `docs/design.md`——这是唯一权威的设计文档，从头讲清楚系统的样子、
   每个部件解决什么问题、以及为什么。
2. **这个仓库怎么用**：往下看"现行系统"和"代码地图"两节。
3. **动 lab 前必读**：`docs/ops_notes.md`（在 `hpft-design` 仓库）——
   平台缺陷病历与实验卫生规则，每一条都有事故背书。


## 现行系统

**TCP 执行面（2026-07-30 修订）**：`tcp/bpf-opt3/hpft_tcp_edt_kern.c` 的
整形债务有 50ms 上限——超限报文丢弃（给 rate-based CC 真实信号、给 fq
排队时延封顶），换代时只做有界赦免；`tools/host/hpft_pace_shim.py` 的
generation 是 per-pair 稳定值（仅首装或速率跳变 >25% 换代）。两者共同
消除了"每次预算下发赦免一次债务"导致的超发（BBR 1.22×）与由此产生的
TSO 突发风暴。

**接收端归属（2026-07-30 修订）**：`tools/dpu/rx_agent.py` 的池子划分对
**部分可见的新成员**（年龄 < mix 窗且已有字节）改用上一拍授予额分摊，
避免新发送方的字节被记到在位者头上、令其吃冤枉折扣。

**数据面自 2026-07-29 起常驻泛化 VxLAN overlay**（P0 定案，见
`results/p0_overlay_20260729/summary.md`）：两台 DPU 的 p1 直配 underlay
172.16.1.x + 遥测 10.1.9.x、MTU 9000，ovsbr-p1 挂 4 个 representor +
vxlan100（**tos=inherit 硬性要求**），VF IP 直连方案原样，p1 保持
发送 200G/接收 100G。hpft/plain/jakiro 三环境共用这层数据面，
`tools/lab_env.sh` 一键切换（只切 CC/agents/DHTB 轴）。rx_agent 默认桥
= ovsbr-p1；registry `headroom=0.08`（3% 队列预警 + ~5% VxLAN 封装税）。

三个服务，systemd 托管，配置来自 `config/lab-registry.json`（**repo
副本是唯一真值**，改政策先改 repo 再下发到两台 DPU 的 `/opt/hpft/`，
接收端 mtime 热加载 policy 段）：

- 接收端 DPU（`ssh hpft-dpu2`，经 sgpu02 跳板）跑 `hpft-rxagent-e`
  （`tools/dpu/rx_agent.py`）：vport 硬件计数器直读 → 三层 water-filling
  → 虚拟队列标记 → 带内遥测。
- 发送端 DPU（`ssh hpft-dpu`）跑 `hpft-txagent-e`
  （`tools/dpu/tx_agent_e.py`）：响应律 → 发送端树 → 执行（RDMA 走
  PCC mailbox，TCP 经 UDP 发给 sgpu01 上的 `hpft-pace-shim` 写 BPF
  map）。
- RDMA 侧执行面是 `tools/dpu/pcc/rp_rtt_template_dev_main.c`（DOCA PCC
  device 代码，跑在 DPA 上）。执行面**只读租户 CC、不改它**：`cc_rate` 是
  PCC 里自实现的 DCQCN 风格状态机（另有 ZTR 可切），原样运行；执行面累积它的
  **下降**成一个相对政策份额的偏离 d，自己把 d 收回 1，下发 `rate = d * level`。
  被自己的整形卡住时（实发 ≈ 已下发）那次降速不计入，否则会自己驱动自己。
  取小值的旧组合 `min(cc_rate, level)` 作为对照臂保留（邮箱 `0xcca 0`）。
  TCP 侧同一形态：`tcp/bpf-opt3/hpft_tcp_edt_kern.c` 读内核 CC 的
  `snd_cwnd*mss`，同一合成器，落到 EDT 时间戳。

**响应律（跟踪-审计）**：接收端把政策裁定与审计账本压成一个目标
$u_f=\hat e_f(1-\gamma s_f)$ 下发，发送端在对数轴上做一阶跟踪
$R_f\leftarrow R_f(u_f/R_f)^{kT}$——无分支、无钳位、律侧只剩一个参数
$k$。遥测每流集合两个数 `{u, r}`，**rx/tx 是双端同步格式，必须一起
下发**。控制周期 1ms（`config/lab-registry.json` 的
`period_ms`），现行参数 `k=20`、`gamma=0.25`、`v_seconds=0.072`
（V=576 Mbit，由 ζ 反解 V=4ζ²γê*/k 定值，与 headroom 无关）。取值理由与收敛闭式见 `hpft-design` 仓库的
`docs/design.md` §3.4/§4.2/§6 与 `docs/design_theory.md`；离线验收
（wire 往返 + 三条收敛闭式 + 账本自愈）跑
`tools/tests/law_check.py`，执行面与 CC 的合成（棘轮的 CC 仍拿满份额、
归因、上限、标定无关性）跑 `tools/tests/couple_check.py`。

## 硬性规则

- PCC device 码（`tools/dpu/pcc/`）改完要
  `meson setup --reconfigure build && ninja -C build pcc/doca_pcc`
  （在 hpft-dpu 上）才会真正生效——`ninja` 单独不重编设备码（dpacc 是
  configure 步），改完不重编是常见坑。
- TCP shaper 叫 "host fq+edt"；DPU 侧 TCP 卸载已暂停（架构性负结论：
  OVS 占据 representor ingress，没有既透明又保持 pacing 语义的挂载点），
  除非用户重提不要重启。
- TCP 执行面的 BPF map 布局改过（`hpft_pair_state` 现在 56 B）。**重新 apply
  之前必须先 `rm -rf /sys/fs/bpf/hpft_tcp_edt`**：pin 还在时 `bpftool prog
  load` 失败，而 `tcp-shaper-apply` 只把失败记进结果、tc 仍指着旧程序——表现是
  改动"部署完了却没生效"，而每一层都报健康。
- dpu2 underlay-p1 上的分类 OpenFlow 规则、遥测通道等运行时状态，
  重启会丢，`rx_agent`/`cc_mode.sh` 的恢复流程会自动重装，不要手工删。
- lab 默认停留态、切换 CC 模式：`tools/cc_mode.sh status`/`dcqcn`/`pcc`
  （用法见脚本头注释）。

## 代码地图

- `tools/dpu/rx_agent.py`、`tools/dpu/tx_agent_e.py` —— 现行两端 agent；
  `tools/dpu/fastfill.{c,py}` —— 三层分配（water-filling + 单遍天花板
  级联），两端共用。C 经 ctypes 加载，`.so` 缺失自动回落纯 Python；
  纯 Python 路径**保留为参照**，`fastfill_test.py` 用它对拍 C。
  两个 agent 的启动 banner 会打印 `fill=C` 还是 `fill=python`；
  `tools/dpu/vport_meter.c` —— 独立于 agent 之外的 1ms vport 计数器
  采样常驻进程（`hpft-vport-meter` systemd unit）。
- `tools/dpu/pcc/` —— DOCA PCC device 代码（RDMA 执行面，跑在 DPA 上）。
- `tools/host/hpft_pace_shim.py` —— host 侧 BPF 写入桥（sgpu01）。
- `tools/tcp_shaper/` —— TCP shaper 库+CLI（`tcp_shaper_lib.py`、
  `tcp-shaper-apply`/`-controller`/`-update-rate`），`hpft_pace_shim.py`、
  `hpft-unified-controller`、几个 `tools/tcp_*` 脚本都从这里 import；
  原是第一代实现（`hpft-exp-deprecated`）的一部分，2026-07-23 完整迁移
  进本仓库，源码原样保留。
- `tools/lab-infra/vf_setup.sh` —— VF 重建脚本（`cc_mode.sh` 的
  `post_recover` 调用）。
- `tools/lab-infra/deploy_check.sh` —— **跑实验前必过**：repo 与两台 DPU
  的代码一致、两个 agent 健康且近期无 traceback。代码不一致是致命的、
  配置不一致只是警告（runner 会推自己的场景配置）。`--deploy` 推送差异
  并在各自 Arm 上重编 `libfastfill.so`。
- `tools/lab-infra/flow_preflight.sh` / `flow_postflight.py` —— 流对可用性
  守卫。RC 的错误完成是**终态**，被打死的 QP 不会自己回来，而每一层都
  还在报健康——这是几次实验产出"看起来正常但结论是错的"数据的原因。
- `tools/cc_mode.sh` —— lab CC 模式切换（DCQCN ↔ PCC+HPFT，GBN ↔ SR）。
- `tools/lab_env.sh` —— 三套实验环境一键切换（`hpft` 生产栈 / `plain`
  固件 DCQCN 基线 / `jakiro` VxLAN+DHTB），编排 cc_mode.sh + 拓扑装拆；
  用法与"fw reset 不清 OVS"等坑见头注释与 `hpft-design` 的 ops_notes.md。
- `tcp/bpf-opt3/` —— TCP 执行面（host fq+edt 的 BPF 实现）。
- `config/lab-registry.json` —— 唯一真值配置。
- `results/<experiment>_<UTC日期>/` —— 工程验证/回归实验产物，每个目录
  一份 `summary.md`；论文评估章节引用的实验在 `hpft-paper` 仓库。
