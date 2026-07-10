# Receiver-Driven 跨类公平控制环 — 设计定稿 v1（2026-07-09）

> **⚠️ 已归档（2026-07-09）**：本方案（A）被方案 E 取代，见
> `rd_fairness_design_e.md`（主设计文档）。A 的侦探臂（Probe/计数器）为
> "看不见竞争者全集"的场景设计，经 scope 对齐后该场景不在论文范围内。
> 存活的决定已在 E 文档 §8 决定日志中标注。本文档仅作历史参考。

状态：**设计已过 grill 评审（16 项决定），实现未开始**。本文档是 EuroSys 投稿的设计骨架，
取代草稿中的判别逻辑与全部 [TODO]。语境：租户 = 实例 = VM；公有云、lossy RDMA、
baseline 无 TC 隔离（TCP/RDMA 同队列）。

## 1. 问题与定位

公有云环境下 RDMA 与 TCP 两类租户流量的公平性：租户间**硬隔离**（$W_i$ 加权份额 +
$MaxRate_i$ 上限），租户内 TCP/RDMA **软隔离**（类权重 + 空闲借用）。

文献坐标：EyeQ (NSDI'13) / Gatekeeper / PicNIC (SIGCOMM'19)（接收端驱动端侧带宽隔离）
+ ElasticSwitch (SIGCOMM'13)（保证 + work-conserving）这条线的延伸。**Novelty**：
(a) 跨异构 CC 类（DCQCN/PCC vs Cubic）的租户内公平——最近邻 Justitia (NSDI'22) 只做
intra-host 应用级 RDMA 公平；(b) 竞争点定位（接收端 vs 交换机）；(c) DPU 租户透明执行
（RDMA 路径全透明）。lossy RDMA 前提由 IRN (SIGCOMM'18)、AWS SRD 等工业趋势背书。

## 2. 公平性定义（Q1–Q4）

**语义（Q3）**：逐瓶颈的层级化带权 max-min（water-filling），受 $MaxRate_i$ 硬上限约束；
不提供绝对最小保证（无 admission control）；论文 claim 措辞为 weighted fair share。

**三瓶颈分类（Q2）**：

| 瓶颈 | 定义 | 观测可见性 | 份额可计算性 |
|---|---|---|---|
| 发送端上行 | 本机上行链路 | 发送端见全集 | 精确（本地调度树） |
| 接收端下行 | 最后一跳朝本机的 egress 口饱和；判据 = 本机总入流量 ≈ LineRate | 接收端见全集 | 精确（两层份额 + per-sender 分解） |
| 其他交换口 | 上游/uplink 拥塞 | 只见子集 | 迭代收敛 best-effort |

host 侧资源拥塞（PCIe/内存，PicNIC 范畴）显式划出 scope。

**接收端结构（Q1, Q17）= 两层公平政策 + 一步 per-sender 分解**：
- **政策层只有两层**：L1 下行在各接收 VM 间按 **dst VM** 的 $W/MaxRate$ 硬隔离；
  L2 接收 VM 份额内按 **dst VM** 的类权重做 TCP/RDMA 软隔离。
- **L3 是机制层分解，不是政策**：份额定义在接收端聚合上，但执行点分布在各发送端，
  且发送端互不可见——聚合 cap 无单一执行点可实施，必须由唯一看见全部来源的接收端
  拆成 per-sender 数额（EyeQ/Gatekeeper 同款）。这正是 Advice 协议里"每条 Rate 只
  约束一个发送端"所隐含的除法；也是聚合超额（per-sender 各自合规、加和穿透 L1/L2，
  即草稿 $R_i > E_i$ policing 问题）的结构性解。拆分策略（Q17）：类份额内对各 src
  按需求 water-filling（需求估计见 §6），不引入新权重，目标纯粹是把份额用满。

**类权重归属（Q4）**：瓶颈属主说了算——上行用 src VM 的类权重，下行用 dst VM 的；
两端配置不一致不构成冲突，实际比例由 binding 瓶颈决定。

## 3. 信号（Q5, Q10）

接收端 DPU：
- 逐 $(src, dst, class)$ 速率计数器；
- RoCEv2 CE 标记比例 + CNP 计数（无 TC 隔离下两类同队列，RDMA 的 CE 是共享队列的
  拥塞金丝雀）；
- **DPU 间沿租户同队列的低频 RTT 探测**（Swift 式类中立信号，补租户只发 TCP 时的
  金丝雀盲区；基础设施自有探测包，不碰租户流量）。

发送端 → 接收端：**cap-hit 位**（fq 队列积压 / PCC level 顶格 = 需求 > cap），随租约
心跳回报；同一信号本地供预算仲裁器即时使用。消除"被 cap 实体需求被遮蔽"的分配冻结死锁。

## 4. 判别表（Q6–Q8）

每个控制周期，每个接收端 DPU：

1. **下行饱和？**（$R_{total} \geq \beta \cdot LineRate$ 连续 $k$ 周期去抖）→
   **接收端竞争**：L1/L2 water-filling 算份额、L3 按需求拆分 → 下发 per-sender Limit 租约。
   **无探测**（$cnt_{end}$ 取消；草稿 $R_i > E_i$ 的 policing TODO 消解——算出的 cap
   即 policing）。
2. **否则，逐路径看拥塞证据**（CE 比例 > θ 或 RTT 抬升）：
   - 某租户类比例偏离 + 受抑制类近期速率非零 → $cnt_{sw}$++ → 达 $k$ → **Probe**
     （γ 折扣短租约）→ 确认 → **迭代 γ 步进 Limit**；未确认 → per-键指数退避。
   - 租户整体受压但内部比例正常（第三方压制）→ **不动作**。
3. **无拥塞证据** → 非竞争：计数器衰减，租约不续租、到期斜坡释放。

边界情形：下行饱和与上游拥塞并存时步骤 1 优先，残余由后续周期步骤 2 收敛。

**不变式**：
- **永不限速受害者**（整体低于份额且比例正常的租户不动作）；
- **强信号直接算，弱信号才做实验**（Probe 只存在于交换机分支）；
- fail-open（见 §5 租约语义）。

残余盲区（诚实写进论文 scope）：肇事者在所有接收端视角下均不超额、但聚合拥塞核心
交换口——需交换机支持（INT/HPCC）或中心化聚合（BwE），非本文目标。补偿机制：对称
部署下每个接收端各管可见部分。

## 5. Advice 协议与租约（Q12–Q14）

Advice $= \{Op, tenant(src\ VM), class, dst, rate, \tau\}$，$Op \in \{Probe, Limit, Release\}$。

- **键 $\{src, class, dst\}$ 天然单写者**（dst VM 只住一台机器，Advice 只来自托管它的
  接收端 DPU）；同键语义 = 新替换旧，发送端无合成冲突。VM 迁移的陈旧租约由 τ 自然过期。
- **Limit（Q12）**：soft-state——拥塞持续则接收端周期续租；不续则到期，到期后发送端
  **斜坡回升**（消除周期 τ 极限环）。Release 仅为提前释放的快速路径优化，正确性不依赖。
  丢续租 = 提前斜坡释放（安全），丢 Release = 晚几周期释放（无害）→ fail-open。
- **Probe**：短 τ、不续租、到期即时恢复（斜坡反而污染观察窗）。
- **发送端组合语义（Q14）**：有效速率 = $\min(\text{稳定树层级份额},\ \text{租约 cap})$。
  租约是 $(tenant, class)$ 节点下按 dst 细分的叶子级 cap，树不动——本地公平性不被远端
  反馈破坏，让出带宽由树自然 work-conserving 回灌（先同类他 dst，溢出给另一类）。
  Probe/Limit 在执行点无差别；Advice 处理退化为租约表增删改。

## 6. 控制律细节（Q9–Q13）

- **water-filling 需求估计（Q10）**：demand = 近期峰值速率 × (1+ε)，被 cap 实体以
  cap-hit 位覆盖为"需求 > cap"。
- **Probe 协议（Q11）**：前置过滤 = 受抑制类近期速率非零。成功判据 = **受抑制类自身
  改善**（速率回升超阈幅 δ；RDMA 辅以其 CE 下降），观察窗口按被观察类校准——RDMA 短窗
  （ms 级），TCP 长窗（多 RTT cwnd 爬升）。注意：拥塞信号消退**不是**判据（只证明偏高类
  贡献拥塞，不证明受抑制类能用上让出的带宽；误判会违反 work-conservation）。阴性结论
  进 per-键指数退避，防止对合法借用的周期骚扰。
- **交换机 Limit（Q7）**：不试图一步算份额（竞争者集合不可见，算不出；草稿用下行 E 值
  是对错误瓶颈算份额）。迭代 γ 步进，步长可随 CE 比例缩放（DCTCP α 式）；信号消退停降
  维持，持续干净斜坡回升。论文定位 best-effort 收敛。
- **防同步（Q13）**：控制周期/Probe 相位加 ±20–30% 随机抖动 + 信号门控停降；残余振荡
  幅度列为 eval 指标。
- **计数器（Q9）**：键 = 租约键 $\{src, class, dst\}$；滑动窗口衰减；Probe 进行中与
  租约生效后 settle 期冻结计数。参数值进实验敏感性分析。
- **流量增减（原草稿 TODO）**：结构性解决，无专门逻辑——新流下周期被 water-filling
  覆盖；类转空闲则 cap-hit 消失、仲裁器回收预算、租约不续自然过期。

## 7. 发送端执行（Q15）

稳定树是概念模型，物理执行分三件：
- RDMA：PCC/DPA caps（租户透明，现有 shaper）；
- TCP：host fq+edt（现有 shaper，非租户透明）;
- **本地预算仲裁器**：DPU agent 纯本地快环（几十 Hz），租户预算在两类 shaper 间按
  权重+需求做二实体 water-filling；需求信号 = fq 积压 / PCC level 顶格，零网络依赖。
  快慢环时间尺度分离（本地 ms 级借用环 ⊂ 远端几十 ms 级公平环）。

透明性定位（论文表述）：执行抽象与数据面无关；原型中 RDMA 路径全透明、TCP 路径 host
辅助；T3.2 负结论（共存模式下 DPU 透明 TCP 卸载不可行）作为 finding 写入。

## 8. 评估（部分定案，Q16）

- **已定 baseline**：B0 无隔离（motivation：两类 CC 共存的系统性不公平）；B1 仅
  per-VM 静态 cap（现行云实践）。
- **推迟到实现后再定**：B3 交换机 TC/DWRR 隔离（最强对手）、消融组（无类层/无借用/
  无 Probe/无 cap-hit/无 jitter）、场景矩阵与规模路径。

## 9. 开放项

- 参数（控制周期 T、k、θ、β、γ、δ、ε、τ、窗口长度）→ 实验敏感性分析，论文给推荐值。
- 待验证 lab 事实：交换机 TC/ECN 可配性；DPU 上 TCP/RDMA 分类计数的可行方式；RTT
  探测包与租户流量同队列的注入方式。
- 可选升级：RTT 梯度调制 γ 步长（Swift/TIMELY 式），放 evaluation 讨论。
- Future work：跨接收端协调协议；host 资源拥塞；min-guarantee 语义分层。

## 10. 决定日志

| # | 问题 | 决定 |
|---|---|---|
| Q1 | 接收端份额索引 | 两层政策（接收 VM 间 → 类间）+ per-sender 机制分解 |
| Q2 | 接收端竞争定义 | 下行饱和（R_total ≈ LineRate），host 资源出 scope |
| Q3 | 公平语义 | 逐瓶颈带权 max-min + MaxRate，上限无保证 |
| Q4 | 类权重归属 | 瓶颈属主（上行 src、下行 dst） |
| Q5 | 信号集合 | 计数器 + CE/CNP + DPU 间同队列 RTT 探测 |
| Q6 | 判别重构 | 接收端竞争直接算并下发，cnt_end 取消，Probe 只留交换机 |
| Q7 | 交换机 Limit | 迭代 γ 步进（可 CE 缩放），非一步到位 |
| Q8 | 第三方压制 | 不动作；永不限速受害者；残余盲区出 scope |
| Q9 | 计数器 | 键=租约键、滑窗衰减、瞬态冻结、参数进实验 |
| Q10 | 需求估计 | 峰值×(1+ε) 推断 + 发送端 cap-hit 反馈 |
| Q11 | Probe 判据 | 受抑制类自身改善，窗口按类校准，阴性退避 |
| Q12 | 租约生命周期 | soft-state 续租 + 到期斜坡；Release 仅优化 |
| Q13 | 防同步 | 相位 jitter ±20–30% + 信号门控 |
| Q14 | 组合语义 | min(树份额, 租约 cap)，树不动 |
| Q15 | 类间借用实现 | 本地预算仲裁器（几十 Hz 二实体 water-filling） |
| Q16 | Baseline | B0/B1 定；B3/消融/场景推迟到实现后 |
| Q17 | L3 拆分策略 | 类份额内按需求 water-filling；L3 定位为机制层分解而非政策层 |

## 11. 文献锚点

EyeQ (NSDI'13)、Gatekeeper (WIOV'11)、PicNIC (SIGCOMM'19)——接收端驱动端侧隔离；
ElasticSwitch (SIGCOMM'13)——保证+借用与 probe 代价教训；FairCloud (SIGCOMM'12)——
逐链路公平定义；BwE (SIGCOMM'15)——层级 water-filling 与需求上报；Seawall (NSDI'11)——
per-source 加权；Justitia (NSDI'22)——RDMA NIC 应用级公平（最近邻）；Swift
(SIGCOMM'20)——endpoint/fabric 拥塞分解（RTT 探测的依据）；IRN (SIGCOMM'18)——lossy
RDMA；DCQCN (SIGCOMM'15)、DCTCP——CC 与 ECN 行为差异（motivation）；HPCC
(SIGCOMM'19)——INT 路线（盲区的指路）；Annulus (SIGCOMM'20)——异构 CC 聚合共享瓶颈。
