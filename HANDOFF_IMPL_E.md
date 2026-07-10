# HANDOFF — 方案 E 实现（2026-07-09）

给接手实现的新 agent 的单一入口。**任务：实现 `docs/rd_fairness_design_e.md`
（方案 E，EuroSys 投稿的核心系统），按其 §5.3 里程碑 M0→M5 推进。**

## 阅读顺序（动手前全部读完）

1. 本文件。
2. `HANDOFF.md` —— 项目级入口：lab 事实、硬性规则、既有系统（RDMA PCC shaper /
   TCP host fq+edt / 统一 controller）的状态与代码地图。**全部继承。**
3. 自动 memory（每会话自动加载）：`hpft-shaper-v2-status.md`（lab 细节与坑）、
   `eurosys-rd-fairness-design.md`（设计演进史）、`tcp-shaper-naming-and-scope.md`。
4. `docs/rd_fairness_design_e.md` —— **实现的唯一设计依据**（含术语表、公式、
   组件职责、里程碑验收标准）。设计已定稿：**实现中不做设计变更**；发现设计
   问题就记录并问用户，不要自行改设计。
5. 参考（按需）：`docs/rd_fairness_design_c.md` §4/§10（fabric 扩展的律 +
   EQDS 对照）；`docs/rd_fairness_design.md`（方案 A，已归档，仅历史语境）。

## 你要做什么 / 不要做什么

**做**：重写 `tools/dpu/rx_agent.py`（→ 虚拟调度器 agent）与
`tools/dpu/tx_agent2.py`（→ 响应律 agent），扩展 `config/lab-registry.json`
schema，按 M0→M5 逐里程碑实现并验收，每个里程碑完成后向用户汇报实测数据。
PCC device 码与 tcp/bpf-opt3 原则上**复用不改**。

**不做**：不改设计；不动 `devlink port function rate`（会 wedge vport，唯一
恢复是 OVS del-port+add-port）；不重启 T3.2/TCP DPU 卸载路线；TCP shaper 叫
"host fq+edt"不叫 opt3；不删 dpu2 上已装的三条分类 OpenFlow 规则（见下）。

**预授权**：实验环境（sgpu01/sgpu02 + 两台 DPU）的扰动性操作（重启服务/改链路
速率/长实验）无需逐次询问。

## 已验证的关键操作（2026-07-09 实测通过，直接照用）

- **逐流集合计数**（M0 的核心，设计文档 §5.2）：dpu2 `underlay-p1` 上已装
  三条规则（`udp,tp_dst=4791` / `tcp` / `ip`，均 `actions=NORMAL`，另有默认
  priority=0 NORMAL）。它们使 offloaded megaflow 达到 (MAC 对, 类) 粒度。
  读取：`sudo ovs-appctl dpctl/dump-flows type=offloaded`（~30ms）。**规则是
  运行时状态，OVS/DPU 重启即失——新 rx_agent 启动时必须自装**（幂等 add-flow）。
  删除需 `--strict del-flows`（不带 --strict 会报 unknown keyword）。
- **incast 制造**（M3）：接收端 DPU
  `sudo ethtool -s p1 speed 100000 duplex full` → ~10s 协商到 100G；恢复
  `speed 200000`。另有 50G/25G 档。发送端保持 200G。link flap 后各流首包
  40-100ms（OVS upcall/重学习）属正常，稳态 0.04ms。
- **RDMA 测试**：两端都用 `~/hyperfront/perftest-26015/ib_write_bw`（系统
  6.23 版 core dump），`-D` 两端一致；接收端 server 先起。vf0 = mlx5_6 →
  10.1.0.2。
- **PCC 构建**（若确需动 device 码，先问用户）：DPU `~/bzx/doca34-apps`，
  `rm -rf build && meson setup build -Denable_all_applications=false
  -Denable_pcc=true && ninja -C build`，然后 `bash /tmp/rp_service.sh start`。
- **RP 内窥**：以 ubuntu 身份 `echo "0xdeb <idx>" > /tmp/rp_fifo`（sudo tee 会
  被 fs.protected_fifos 挡），读 `/tmp/pcc_rp.log` 的 `HPFT_RSP`。
- **服务**：发送端 DPU（`ssh hpft-dpu`）：`hpft-txagent`（systemd）+ RP
  （`/tmp/rp_service.sh`）；接收端 DPU（`ssh hpft-dpu2`，经 sgpu02 跳板）：
  `hpft-rxagent`（systemd）。DPU ubuntu 免密 sudo；host sudo 受限。
- **拓扑**：链路全 200G。dpu0 两 host **直连**（SF 网 10.0.4.201/202，agent
  遥测走这里——与数据路径物理分离，正合设计 §3.4）；dpu1 过**同一台交换机**
  （VF 网 10.1.{0..3}.{1,2}，实验数据路径）。sgpu01 其余 BF3 不可用。

## 已知 bug（实现中必须处理）

- **tx_agent2 长跑卡死**（2026-07-09 发现）：运行约 4 天后 mailbox 只更新
  pair 3，新 QP 卡在 min-level 地板（bud/128≈47Mbps，ib_write_bw 表现为
  0.044G）。`systemctl restart hpft-txagent` 即愈。**根因未查**。你在重写
  tx_agent 时必须定位并根治（嫌疑方向：计数器 fd 失效、FIFO 重开逻辑、
  或 per-pair 循环内异常吞掉）；把根因写进交接记录。

## 里程碑与汇报

按设计文档 §5.3 的 M0→M5 顺序，验收标准以该表为准。节奏要求：
- 每个里程碑：实现 → 实测 → 结果对照验收标准 → **向用户汇报数据**（速率曲线/
  误差/收敛周期数）→ 得到确认再进下一个。
- 实验产物放 `results/<probe>_<UTC日期>/` + `summary.md`（沿用项目惯例）。
- M1 起的每个里程碑都是论文的候选图，保存原始数据。
- 参数值（§5.4）实现时定初值，M4 做敏感性分析后给推荐值。

## 决策边界

设计问题（发现公式错误、机制矛盾、验收标准不合理）→ 记录 + 问用户，不自行改。
工程问题（代码结构、语言选择、进程托管方式、调试手段）→ 自行决定，遵循项目
现有惯例（Python agent、systemd 托管、SO_REUSEADDR、keepalive wrapper 教训见
`docs/c1_controller.md`）。
