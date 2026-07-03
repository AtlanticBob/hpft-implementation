# Phase 1:DOCA PCC 可行性探测执行计划

日期:2026-07-03。执行环境:sgpu01(host)+ BF3 DPU(hpft-dpu)+ sgpu02(对端)。

## 1. 目标

回答"DOCA PCC 能否作为 RDMA shaper v2 的执行点"。这是整个 RDMA v2 路线的
go/no-go 决策,探测项按风险从高到低排序:

- **Q1(核心,go/no-go)VF 覆盖**:部署在 DPA 上的 PCC 算法,能否控制 host 侧
  VF(租户视角的 vNIC)发出的 RoCE 流量速率?租户侧是否确实零改动、零感知?
- **Q2 流标识**:PCC 事件/flow context 里能拿到什么标识?算法侧能否区分
  源 function(VF)与目的地,从而建立 flow → `{src_vnic, dst_vnic}` 映射?
  不能则设计带外映射方案。
- **Q3 控制路径**:Arm 控制进程 → mailbox → DPA 算法 → 硬件生效,端到端延迟、
  可持续更新频率(目标 ≥100 Hz)、以及 cap 的速率精度如何?
- **Q4 共享 cap**:在算法内维护 per-pair 聚合状态,多 QP/多进程共享一个 cap
  是否可行(SCR 的 fair scheduler 模式)?
- **Q0(贯穿)安全与回滚**:doca_pcc 替换的是整卡 CC;确认停止进程即恢复固件
  CC,记录影响范围与回滚 SOP。

## 2. 已确认的环境事实

- `USER_PROGRAMMABLE_CC=1` 已开启,**无需** mlxconfig 改动和固件 reset,Phase 1
  没有必须的扰动性准备步骤(这是本计划最大的减险项)。
- DOCA 2.9.3008 + 完整 dpacc 工具链在 DPU Arm 上就绪;PCC 参考应用
  (`/opt/mellanox/doca/applications/pcc/`,含 rp/np device 代码与 host 控制程序、
  `pcc_params.json`、`build_device_code.sh`)可直接构建。
- 两端 RDMA 冒烟通过:vf0↔vf0 ib_write_bw 196 Gb/s;两端 perftest 用
  `~/hyperfront/perftest-26015/ib_write_bw`。
- sgpu01/sgpu02/hpft-dpu 三点 ssh + (DPU) passwordless sudo 均可用,单人可
  独立编排全部实验。

注意:SCR 论文用的是 DOCA 2.6,本机是 2.9,API 有演进(通常更完整),对照
`/opt/mellanox/doca/include/doca_pcc*.h` 与 device 头文件为准,不照搬论文细节。

## 3. 分步计划

### S0 环境盘点与基线(0.5 天)

操作:
- DPU:`doca_caps` 输出、PCC 应用构建依赖(meson/ninja)、`dpa-ps`/PCC counter
  等可观测性手段盘点;记录当前 ECN/CNP 相关计数器与 QoS 配置作为原始状态。
- 基线测量并存档:vf0↔vf0、vf1↔vf1 单 QP / 64 QP / 4 进程的全速吞吐与延迟
  (ib_write_bw / ib_send_lat),作为后续所有 probe 的对照。
- 写回滚 SOP:停止 doca_pcc → 固件 CC 自动恢复;验证方法;卡死时的恢复手段
  (最坏情况 `mlxfwreset`,需 maintenance window)。

产出:`docs/s0_inventory.md`、`results/s0_baseline_<date>/`。

### S1 参考 PCC 应用原样构建与运行(1 天)

操作:
- 把 PCC 参考应用拷入本仓库 `pcc/`(保留 NVIDIA 许可头),
  `build_device_code.sh` 编译 RP device 侧,meson 构建 host 侧。
- 在 DPU Arm 上以 NVIDIA 参考算法(DCQCN-like)运行 doca_pcc,期间从 sgpu01
  VF 打满流量,确认:进程稳定、无固件报错、吞吐相对基线无显著回退。
- 停止 doca_pcc,复测流量,验证回滚干净。

验收:参考算法运行下基线吞吐回退 <5%;启停 10 次无异常。
风险:2.9 的 PCC 应用可能要求同时跑 NP 侧或特定端口绑定——以 `pcc_params.json`
和应用文档为准;缺依赖则 `apt install doca-sdk-pcc` 等补齐。

### S2 Probe A:VF 覆盖 + 硬限速(1–2 天)★ go/no-go

操作:
- 最小改动参考 RP 算法:忽略 CC 输入,把所有 flow 的 rate 恒定钳到 8 Gbps
  (先硬编码,不做 mailbox)。
- sgpu01 vf0 → sgpu02 跑 `ib_write_bw -D 30`,观测吞吐是否被钳到 ~8 Gbps;
  再用 vf1 与多 QP(`-q 64`)复测。
- 全程 host 侧不安装、不配置任何东西(租户零感知的直接验证)。
- 若不生效:检查 PCC 的 function/port 作用域(PF only?)、PPCC 寄存器状态、
  NP 侧要求,给出根因结论。

验收(GO 判据):VF 流量被 DPA 设定值钳制,误差 ≤5%;host 侧零改动。
NO-GO 时的备选路线:hairpin + DevX SQ packet pacing 探测(调研报告 §3c)。

**重要先验(2026-07-03,来自用户)**:5/28 的 `pcc_fixed_rate_10g` 实验对
**PF** 流量限速成功(177→9.18 Gbps),但当时**对 VF 流量施加限速似乎无效**。
S2 因此是真正的未决问题,必须显式分开测试 PF 与 VF 两种流量;若 VF 确实不在
PCC 覆盖范围,需要查证:VF QP 的 CC 事件是否路由到 DPA(可能需要 fw 配置,如
per-function CC 使能/vport CC 归属)、DOCA 3.4 是否改变此行为,再决定 go/no-go。

### S3 Probe B:流标识与 pair 映射(2 天)

操作:
- 在 RP handler 中导出 per-flow 可见字段(flow context id、event 携带字段,
  对照 `doca_pcc_dev_*.h`),经 host 侧 trace/counter 通道带出。
- 实验矩阵:{sgpu01 vf0, vf1} × {sgpu02 vf0(10.1.0.2), vf1(10.1.1.2)} 四条并发流,
  验证算法侧四条流是否可区分、可归类到正确的 src/dst。
- 结论输出映射方案,候选按优先级:
  1. 纯 DPA:事件字段足以区分 src function + dst;
  2. Arm agent 带外:控制面从 QP 建连/GID 表构建 flowtag→pair 映射下发;
  3. eSwitch flow tag 辅助标记。

产出:`docs/s3_flow_identity.md`(含最终映射设计)。

### S4 Probe C:控制路径延迟 / 频率 / 精度(2 天)

操作:
- 实现 per-pair cap 表(DPA 全局内存)+ mailbox 更新;Arm 侧写一个最小控制
  进程(命令行 + JSON-lines,形态对齐 v1 的 tcp-shaper-controller)。
- 复刻 v1 实验形态:单条活跃流,cap 走 8→4→12→6→8 阶梯,吞吐按 100 ms bin
  记录,测"下发→吞吐生效"延迟;再跑 10 Hz / 100 Hz 交替 cap(7/9 Gbps)
  持续 60 s,统计达成频率与更新延迟分布。
- cap 精度扫描:1/2/4/8/12/16 Gbps 各 30 s,记录达成值误差。

验收:100 Hz 稳定;生效延迟 ≤10 ms;cap 误差 ≤5%(SCR 提示 Mbps 级精度困难,
目标精度定在百 Mbps 量级)。

### S5 Probe D:多 QP / 多进程共享 cap(2 天)

操作:
- 算法内实现 per-pair 聚合:先做静态均分(cap/活跃流数),再做简化 water-filling
  (SCR §6.2 模式:欠载流让出、满载流回收)。
- 同 pair:1/2/4 进程 × 1/64/256 QP,验证聚合吞吐 ≈ cap;
- 负对照:不同 pair 互不影响;未配置 pair 的流量不受限(fail-open 语义)。
- 压力点:1024 QP 下算法稳定性(对齐 v1 的验收线)。

验收:聚合误差 ≤10%;1024 QP 稳定;fail-open 成立。

### S6 探测报告与 Phase 2 决策(0.5 天)

- 汇总 S2–S5 数据,写 `docs/phase1_report.md`:go/no-go、Q1–Q4 答案、
  Phase 2 设计要点(生产算法 = 参考 DCQCN + `rate = min(cc_rate, pair_cap)`、
  pair 映射方案、controller 接口)。

## 4. 实验纪律

- doca_pcc 运行期间**整卡 CC 被替换**(含所有 VF):S1 起的所有运行段在 lab
  独占时段执行,开始/结束在实验记录中标注时间窗。
- 每个 probe 的原始输出进 `results/<probe>_<UTC日期>/`,附 `summary.md`
  (沿用 v1 的方法学:记录命令、环境、负对照);probe 代码允许一次性脚本,
  不做 v1 那种 runner 级过度工程。
- 不改 host(sgpu01/sgpu02)任何网络/驱动配置;DPU 上除运行 doca_pcc 外
  不改 OVS/offload 状态(TCP 侧是后续 phase 的事)。

## 5. 风险表

| 风险 | 概率 | 应对 |
|---|---|---|
| PCC 不覆盖 VF QP(Q1 失败) | 低(SCR 已在 SR-IOV VF 环境验证) | 尽早跑 S2;失败则转 hairpin+SQ pacing 探测 |
| DOCA 2.9 与论文 2.6 API 不一致 | 中 | 以本机头文件/样例为准;S1 先原样跑通再改 |
| NP/RP 部署形态不明(是否需对端配合) | 中 | S1 中确认;对端 sgpu02 无 DPU 也可先做单向 RP |
| DPA 调试手段有限 | 中 | 用 host trace/counters + dpa-gdbserver;探测代码多埋计数器 |
| 速率精度不足 | 低 | 目标精度定百 Mbps 级;记录实测曲线供 Phase 2 定标 |
| 运行中固件/驱动异常 | 低 | 回滚 SOP(S0);最坏 mlxfwreset,排 maintenance window |

## 6. 里程碑

S0+S1(≈1.5 天)→ S2 go/no-go(≈2 天)→ S3–S5(≈6 天)→ S6 报告。
总计约 2 周(单人,含实验窗口协调余量)。GO 则进入 Phase 2(RDMA shaper v2
生产形态);NO-GO 则启动备选探测并复用本计划的基线与方法学。
