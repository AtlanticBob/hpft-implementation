# HPFT 论文：Design 与 Implementation 章节草稿（v1）

> 约定：章节编号按"1 引言、2 动机、3 设计、4 实现、5 评估"暂定，交叉引用
> 里的 §2/§5 均为占位。【图 N】/[Figure N] 为插图占位。所有具体参数值
> 集中在实现章的表 2；设计章原则上不出现数字。中文在前、英文在后，
> 两版逐节对应。

---

# 中文版

## 3 设计

### 3.1 概览

HPFT 在网络边缘提供两层公平：租户之间按购买额度的硬隔离，以及租户内部
TCP 与 RDMA 两类流量之间加权的、work-conserving 的共享。设计受两条现实
约束支配：租户的协议栈不能改动，交换机也不能被要求提供 per-tenant 的
队列或调度。于是一切机制都只能落在基础设施域内——连接两端的 DPU 上——
并且只能用两类传输都已经理解的信号去驱动它们。

出发点是一个观察：运营商最关心的瓶颈，恰恰不产生任何拥塞信号。一个买了
20 Gb/s 额度的 VM 向空闲的 100 Gb/s 链路发送时，没有排队、没有丢包、
没有 ECN——稀缺是合同性的，不是物理性的。而在物理信号确实存在的地方
（比如 incast），TCP 和 lossy RDMA 对同一个信号的解读又截然不同：DCQCN
见到 ECN 毫秒级退避，Cubic 要等到真丢包才动作。两种情形殊途同归：
"谁拿多少"成了拥塞控制算法碰撞的副产品，和任何人的政策意图无关。

HPFT 用一个动作同时回应这两个问题：**信号由资源的属主自己造出来**。
系统里每个受控资源都有一个天然的属主——发送端的上行链路归发送方主机，
目的 VM 的已购份额和接收端的下行链路归接收方主机——谁的资源，其内部
如何划分就由谁的政策说了算。属主在自己的 DPU 上为资源记一本账：每个
控制周期，它按政策把资源容量在当前的竞争者之间划分出各自的应得速率；
对每个竞争者维护一个虚拟队列——没有真实报文在里面排队，它只是对
"到达速率超出应得速率"的差做时间积分——并把队列长度归一化成一个标记
值回传给发送端。虚拟队列这个构件可以追溯到 HULL 的 phantom queue，
但那里它长在交换机上、为的是制造带宽余量；在 HPFT 里它长在边缘、承载
的是层级化的租户政策。

发送端这边只有一条响应律，所有流量——不分租户、不分类——都跑同一条：
收到零标记就温和上探，收到标记就按比例退避。这条律刻意不带任何权重；
所有的差异化都发生在标记的铸造端。权重住在属主的调度器里，于是改政策
只需要改属主一侧的账本，发送端一无所知；也正因为律处处相同，一个流
穿过多个受控资源时不需要任何跨属主的协调——每个属主按自己的账本标记，
发送端响应当下正在响的那个信号，速率自然收敛到各处应得份额中最小的
一个。这一点并不平凡：不同属主各自归一化的权重本来就不可比，幸而在
这个架构里它们从头到尾不需要被比较。

这样一来，两种异构性在同一个机制面前消失了。政策瓶颈和物理瓶颈在标记
面前不可区分——账本把合同稀缺合成为和物理稀缺一样的信号；TCP 和 RDMA
在响应律面前不可区分——律只认标记，不认传输。连类间借用也不需要专门
的机制：一类空闲时，账本的 work-conserving 划分自然把它的份额划给
另一类，借用类无标记地涨上去；需求回归时份额重划，标记再把借用者
压回去。

【图 1：HPFT 控制环。接收端 DPU 每周期测量各流集合的到达速率 r_f，
经层级 water-filling 得到应得速率 e_f 与公平上限 ê_f，更新虚拟队列
得到标记 s_f，随遥测回传；发送端 DPU 对每个流集合跑响应律维护速率
上限 R_f，最终 pace_f = min(R_f, Tree_f) 下发执行器；租户自己的拥塞
控制在 pace 之下照常运行。】

一圈控制环如图 1。接收端每周期做三件事：测量每个流集合的到达速率，
用层级 water-filling 算出它此刻的应得速率，把持续的超额积分成标记发
回去（§3.3、§3.4）。发送端收到遥测后跑响应律更新速率上限，再与本地
政策树的份额取最小值交给执行器（§3.5、§3.6）。租户的拥塞控制始终跑
在 pace 之下：实际发送速率是租户 CC 想发的和 pace 中较小的一个，
亚周期尺度的突发由租户 CC 自己兜住。

### 3.2 服务模型与政策语义

**部署模型。** 每台主机配一块 DPU，租户 VM 经 SR-IOV 虚拟功能（VF）
收发，全部流量都经过 DPU 的 eSwitch。DPU 属于基础设施域，租户不可见、
不可改。每个 VF 有唯一 MAC，因此在 DPU 上一对（源 MAC，目的 MAC）就
唯一确定一对（源 VM，目的 VM）。

**流集合。** 系统观测与限速的最小单位是**流集合**（flow-set）：三元组
{源 VM，目的 VM，类}，类取 TCP 或 RDMA。VM1 发往 VM5 的全部 RDMA 流量
是一个流集合；系统不再细分到单条流或单个 QP——政策以流集合为单位表达，
控制状态也以流集合为单位维护，这使控制面的开销只随流集合数增长，与
QP 数无关。

**政策参数。** 运营商与租户的意图归结为四类参数（表 1），每类参数有
唯一的属主和唯一的生效位置。

| 参数 | 含义 | 谁配置 | 在哪生效 |
|---|---|---|---|
| $W_i$ | 租户 $i$ 的租户间权重 | 运营商 | 属主调度器的 VM 层 |
| $MaxRate_i$ | 租户 $i$ 购买的带宽上限 | 运营商 | 发送端树（发出上限）与接收端调度器 VM 层（接收上限） |
| $W_{i,c}$ | 租户 $i$ 的类间权重 | 租户 $i$ | 属主一侧：发送端树用源 VM 的，接收端调度器用目的 VM 的 |
| $u_{d,c,s}$ | 目的 VM $d$ 对类 $c$ 中源 $s$ 的 per-sender 权重 | 租户 $d$ | 接收端调度器的流集合层（默认全 1） |

公平性以链路字节（wire bytes）为口径定义——$MaxRate$ 出售的就是链路
上的字节。

**保证范围。** HPFT 对**边缘瓶颈**给出稳态保证：分配收敛到政策定义的
层级加权 max-min 份额。边缘瓶颈指有明确属主、且属主能看见其上全部竞争
流量的瓶颈，共三类：发送端上行链路（所有流量从本机发出）、目的 VM 的
政策份额（所有流量落到本机）、接收端下行链路（incast——N 打 1 的速率
失配唯一物化的位置是最后一跳朝本机的出口，本机收到的不会超过线速，
故接收方全可见）。

两点界定需要说清。其一，类间权重是 work-conserving 的目标份额：当某类
自身的拥塞控制在物理拥塞的链路上顶不满它的加权份额时，差额会被借给
兄弟类，而不是空置——这是借用语义的直接推论，对于要求物理拥塞下也严格
执行权重的运营商，可以对该类关闭借用，代价是利用率。其二，明确不在
保证范围内的：核心网瓶颈（没有属主能看全，租户 CC 按现状应对）、丢包
的消除、路径负载均衡，以及短于一个控制周期的突发精度（由租户 CC 兜底
——DCQCN 的 CNP 响应是亚毫秒级的，始终跑在 pace 之下）。

### 3.3 接收端：测量与应得速率

接收端的第一件事是把"现在谁在以多快的速度到达"测准。这里的要求是
既新鲜（每周期一个可用读数）又能按类拆分（TCP 与 RDMA 分开），而这
两个属性在现成的计数器上恰好各缺一半：DPU 上按（MAC 对，类）细分的
流表计数器能按类拆分，但驱动对它的刷新太慢，不能用于逐周期控制；
per-VF 的端口计数器任意频率都新鲜，却只有总量、不分类。打通两者的是
一个简单的事实：RDMA 完全绕过内核，主机内核路径的字节计数器只看得见
TCP。于是接收端主机上一个轻量的导出器持续把各 VF 的内核 TCP 字节数
推给 DPU，DPU 每周期用新鲜的端口总量减去 TCP 所得即为 RDMA——两类
速率从此都新鲜。慢的流表计数器降级为类内多发送方的构成比细分，兼作
导出器故障时的回退（§4）。

拿到各流集合的到达速率 $r_f$ 后，先做需求估计：

$$D_f = r_f\,(1+\delta).$$

小余量 $\delta$ 的作用是解锁：一条被抑制的流当前 $r_f$ 很低，若直接
拿 $r_f$ 当需求，分配会把它锁死在低位；乘上 $(1+\delta)$ 给它"想涨
就能涨"的空间——它涨了，下一周期的需求随之涨，直到顶到公平份额。
这个余量必须小：water-filling 的账本是零和的，多分给恢复中的流而它
一时用不掉的部分，恰好是从兄弟流可以实际使用的份额里扣出来的。恢复
的加速不在需求侧做，在发送端做（§3.5 的快速恢复）。

然后是三层带上限的 water-filling——按权重注水、需求小的先满、余量
继续按权重分给未满足者：

- **VM 层**：把根容量 $C_{root}$（下行线速留出小比例 headroom）在
  各目的 VM 间按 $W_d$ 注水，每个 VM 封顶于 $\min(MaxRate_d,$ 其下
  需求之和$)$，产出 $C_d$；
- **类层**：把 $C_d$ 在 {TCP, RDMA} 间按 $W_{d,c}$ 注水。某类需求
  不足其权重份额时，余量归另一类——借用即由此产生，不需要任何显式
  协议；产出 $C_{d,c}$；
- **流集合层**：把 $C_{d,c}$ 在各源之间按 $u_{d,c,s}$ 注水，产出
  每个流集合的应得速率 $e_f$。

headroom 是刻意让分配总额低于物理容量的余量，作用是让虚拟队列在
交换机的物理队列之前先报警。

调度器同时算第二个量，**公平上限** $\hat e_f$：同一棵树，把该流集合
自己的需求设为无穷、其余保持实际值，water-filling 分给它的量。恒有
$\hat e_f \ge e_f$。两者的差别在于：$e_f$ 是需求封顶的，通过 $D_f$
依赖这条流自己的 $r_f$；$\hat e_f$ 与这条流自己的行为无关，只由政策
和别人的需求决定。凡是"参照物不能被流自己的现状拖着走"的地方——
虚拟队列的排空（§3.4）和探测的上界（§3.5）——用的都是 $\hat e_f$。
全部流集合的 $\hat e_f$ 由一个单遍级联算法一次算出，代价 $O(N\log N)$，
与逐条重算严格等价（§4）。

### 3.4 接收端：虚拟队列标记与遥测

每个流集合一个积分器，按超额充入、按未用的公平上限排空：

$$vq_f \leftarrow \mathrm{clip}\!\big(vq_f + \Delta_f\, T,\ 0,\ V\big),
\qquad
\Delta_f = \begin{cases}
r_f - e_f, & r_f > e_f,\\[2pt]
-(\hat e_f - r_f), & r_f \le e_f,
\end{cases}$$

标记是队列长度的归一化：$s_f = \min(vq_f / V,\ 1)$，$V$ 是标记满刻度。
注意虚拟队列积累的是速率对时间的积分，所以 $V$ 是一个固定的比特量，
与控制周期无关。用积分而不是瞬时比值，是为了容忍单周期的测量噪声、
只对持续的超额报警——这是 AVQ 一脉的思路。

充入与排空采用不同的参照物，是这个机制里最值得解释的一处。对称的
选择——低于 $e_f$ 时按 $e_f - r_f$ 排空——有一个隐蔽的陷阱：$e_f$
是需求封顶的，它贴着 $r_f$ 走，于是一条被深度压制的流无论跌到多低，
排空速度都只有实际速率的 $\delta$ 那么一点，积满的队列在低速率下要
几分钟才排得空，这期间标记持续满格，流被永久钉死在地板上。换成公平
上限 $\hat e_f$ 后陷阱消失：$\hat e_f$ 不随这条流自己塌陷，被压制的
流"没用掉的份额" $\hat e_f - r_f$ 很大，队列迅速排空、标记解除；而
恰好用满份额的流（$r_f \approx \hat e_f$）排空速度趋于零，不会平白
获得解除标记的机会。完全空闲的流（$r_f = 0$）既不充入也无从排空，
其队列在一个周期内直接清零——不再发包的流放弃自己的拥塞主张，陈旧
状态不应在它恢复发包时继续惩罚它。

这一个机制统一了三个本来面貌迥异的场景。政策份额争夺：物理零拥塞，
但 VM 层的 $MaxRate$ 封顶使超额流集合出现 $r_f > e_f$，账本把合同
稀缺合成了出来。incast：根容量被顶穿，短缺按权重摊进每个 $e_f$，
headroom 保证虚拟队列先于物理队列报警。借用回收：空闲类需求回归、
份额重划，借用者的 $e_f$ 下移、标记升起。三个场景在标记面前是同
一个场景。

在单个受控资源上，这就是"按超额差分标记的 AQM + 各源 AIMD"的经典
设定：超出 $e_f$ 的被持续标记、乘性减，低于的不被标记、加性增，均衡
点即 $r_f \approx e_f$，而 water-filling 保证 $e_f$ 就是政策份额。
HPFT 的律叠在租户自己的拥塞控制之上，两环不互相激振：pace 只延迟、
不丢包，对租户 TCP 表现为一根平滑的管道；RDMA 一侧执行器语义即
min(DCQCN, budget)，租户 CC 的瞬态响应始终快于且低于 pace。

**遥测。** 接收端每周期对每个在场的流集合发一条定长记录
$\{f,\ s_f,\ r_f,\ e_f,\ \hat e_f\}$。有两个语义值得强调。其一，
$s_f = 0$ 也要发：零标记是"新鲜且无拥塞"的显式许可，发送端的加性增
以收到它为前提，缺席不等于许可。其二，在场即发：只要流集合还在
（转发表项仍存在），哪怕这一刻测到的速率是零也持续发送许可——若只对
$r_f > 0$ 的流发遥测，一次瞬时零读数就会让发送端误入 fail-open、
把速率冲到本地上限，流量恢复后又被打回，循环往复。发送端速率上限
的维护需要 $e_f$ 与 $\hat e_f$（§3.5），把它们随标记一并回传，遥测
就成为响应律唯一的输入通道。

### 3.5 发送端：响应律

发送端为每个流集合维护一个速率上限 $R_f$，每收到一条遥测更新一次，
执行值取本地政策树份额与它的较小者：$pace_f = \min(R_f,\ Tree_f)$。
$R_f$ 的初值即 $Tree_f$：新流集合一上来就拿到发送端政策允许的全部
额度，不做慢启动——若它落在拥塞资源上，标记一两个周期内就到，乘性减
把它压下来，期间的瞬态由租户 CC 兜底，且始终有界于售出额度（§3.6）。

律的骨架是不带权重的 AIMD：零标记加性增，有标记乘性减。骨架之外，
每个分支的形状都值得交代。

**向上探测锚在公平上限。** 连续收到零标记说明这条流长期低于可用份额，
加性增的步长随之逐级放大（借用 DCQCN 的 Hyper Increase 结构，有封顶）。
但探测必须有上界，否则单流期一次瞬时下探就能让放大后的探测把 $R_f$
一路冲穿分配值，随后队列饱和、标记风暴、深砍，形成慢周期的弛豫振荡。
上界锚在公平上限上：

$$R_f \le \hat e_f + \min\!\big(\theta_{p}\,\hat e_f,\ \
g_{p}\cdot\max(0,\ \hat e_f - r_f)\big),$$

其中 $\theta_p$ 是一个小的比例余量、$g_p$ 是余量增益。这个式子的形状
由它要补偿的对象决定：余量存在的唯一理由，是执行器可能欠兑现——pace
设为 $\hat e_f$ 而线上只兑现九成时，探测需要允许 $R_f$ 略高于
$\hat e_f$ 才能把份额真正用满。于是让余量随 $r_f \to \hat e_f$ 衰减
到零：欠兑现的执行器拿到与缺口成比例的补偿，而完美兑现的执行器稳态
收敛在恰好 $\hat e_f$，不会制造持续的超额去慢慢充满虚拟队列。上界的
锚必须是 $\hat e_f$ 而不是 $e_f$：后者经由需求估计依赖发送端自己的
$r_f$，执行面一点松弛就会把不动点拖向零——与 §3.4 排空参照物的选择
是同一个道理。

**向下的剂量按墙钟时间定义。** 标记若持续多个周期，发送端会连续乘性
减。为使一段固定墙钟时间内的总削减与周期选择无关，削减定义为按时间
的指数衰减，按周期离散化：

$$R_f \leftarrow R_f\,(1-\beta\, s_f)^{\,T/\tau},$$

$\tau$ 是律的衰减时间常数。同时削减设有下界：$R_f$ 永不被砍到其公平
上限（以及当前测得速率）的一个固定比例之下。类进入、退出的转换期会
有短暂的标记风暴，若没有下界，连续的乘性减会把 pace 压到执行器难以
恢复的深度；公平上限是政策量、不随测量塌陷，无论标记持续多久，下界
都站得住。

**快速恢复只送回份额之内。** 一次深砍之后光靠加性增爬回太慢。发送端
为每个流集合记住最近一次砍之前的值 $R_{good}$；此后每收到一条零标记
遥测，就令 $R_f \leftarrow (R_f + R_{good})/2$，二分逼近、数个周期
回到砍前水平。若那次标记只是测量噪声，流量很快弹回；若约束是真实的，
下一次乘性减会把 $R_{good}$ 更新为更低的砍前值，恢复目标自然下移，
不与约束对抗。反弹目标同时封顶于上文的探测上界——接收端的 $e_f$ 有
一两个周期滞后，若恢复一步弹到远高于当前份额，立刻超额、立刻被标记、
再次被砍，反而锁死在低位。原则与探测一致：快速恢复把流量送回它的
份额之内，份额之外的部分交给慢速的加性增。

**app-limited 的流被冻结在实际速率附近。** 若一条流的实际速率持续
低于其份额的一个比例，说明应用自己没有用满额度，$R_f$ 被冻结在
$r_f$ 略上方、停止加性增，待 $r_f$ 回升贴近再解冻。虚高的 $R_f$
没有意义，只会在拥塞来临时多挨几拍削减。判别的参照是这条流的份额
$\min(R_f, e_f)$ 而非 $R_f$ 本身——一条已把份额用满、只是被上限
挡住的流不是 app-limited，不应被钳制。

**遥测断流时 fail-open。** 对某流集合连续一段时间收不到遥测，先冻结
$R_f$（停止加性增）；持续更久则把 $R_f$ 斜坡回升到 $Tree_f$。语义是：
接收端故障时，流量退化为只受发送端本地政策约束——租户不被卡死，也
不失控，因为租户自己的拥塞控制仍在，而 $Tree_f$ 有界于售出额度。

### 3.6 发送端：本地树与执行

发送端上行是发送方自己的资源，属主看得见全部竞争者，不需要经过标记环
——直接本地调度。这里遵循云售卖的基本约束：$MaxRate_i$ 是卖给租户 $i$
的额度，任何时刻任何 VM 都不得超过自己买的；某 VM 少用时省下的容量
可以让其他 VM 各自在 $MaxRate$ 之内多拿（超售场景），但谁都不能突破
购买额度。例如线速 100 Gb/s 超售八个 20 Gb/s 的 VM：全部打满时各拿
12.5；一个只用 5 时，其余七个各拿约 13.6——仍在 20 之内。

实现为两层。第一层是 per-VM 的硬件限速器，类盲、静态，作用是软件层
失效时的兜底。第二层是一棵软件树：与接收端同一套 water-filling 跑在
上行容量上，VM 层封顶 $\min(MaxRate_i,$ 需求和$)$——售卖约束由这个
封顶直接保证——VM 内类间按类权重划分。每个流集合的 $Tree_f$ 取它在
这棵树上的公平上限：$R_f$ 以 $Tree_f$ 起步、fail-open 又斜坡回升到
$Tree_f$，两处都要求它是"政策允许的额度"而非需求封顶的份额——否则
新流以 $r=0$ 起步会得到趋近于零的 $Tree_f$，自相矛盾。类间借用就是
这棵树 work-conserving 填充的自然结果，与接收端一致。

最终的 $pace_f = \min(R_f,\ Tree_f)$ 交给两个执行器：RDMA 流集合
下发为 NIC 可编程拥塞控制引擎里的 per-flow-set 预算，由硬件 pacing
执行，租户透明；TCP 流集合下发为主机侧的 pacing 速率（fq qdisc +
earliest-departure-time 时间戳）。两个执行器之下，租户自己的拥塞
控制照常运行：实际发送速率 = min(租户 CC 想发的, $pace_f$)。

## 4 实现

**组件。** 控制面共约 1800 行 Python：接收端 agent（832 行，DPU Arm
核上 systemd 托管）负责测量、调度、标记、遥测；发送端 agent（675 行）
负责响应律、发送端树与执行下发；接收端主机上的速率导出器（88 行）与
发送端主机上的 pace shim（116 行）是两个轻量辅助进程；单遍公平上限
算法（fastfill，73 行）为两端共用，与逐流集合重算 water-filling 的
朴素实现在 12,600 个随机用例上对拍验证严格等价。TCP 执行器是约 250
行 BPF C（clsact + fq，EDT 时间戳）；RDMA 执行器复用 NIC 的可编程
拥塞控制引擎（运行于 DPU 的数据通路加速器上），HPFT 对它的改动仅限
预算接口与一处地板守卫。全部政策与参数集中在一个 JSON registry，
两端 agent 按修改时间热加载，改政策不需要重启任何组件。

**测量路径。** 类级速率由两种硬件计数器拼合（§3.3）。per-VF 的
representor 端口计数器亚毫秒级更新但类盲；按（MAC 对，类）细分的
卸载流表计数器可分类，但驱动以秒级批量刷新，只能用于慢速用途。类
拆分依赖的事实是 RDMA 绕过内核路径：主机导出器每 5 ms 把各 VF 的
内核 TCP 字节数连同读取时刻的时间戳推给 DPU（两者一起打戳以保证
对齐），DPU 每周期以端口总量减 TCP 得 RDMA。速率一律在固定样本数
（约 20 ms 宽）的窗口上计算——固定时间窗对循环抖动不健壮：某周期
稍晚，旧样本掉出窗口便产生除零与双倍间隔的毛刺；固定样本数则始终
平滑掉计数器的更新量化与传输层毫秒级的突发结构。流表计数器降级为
类内多发送方的构成比细分；导出器报告超时未达时，agent 自动回退到
流表归因，测量不因辅助通道故障而失效。

**控制环。** 控制周期 $T$ = 1 ms，接收端主循环实测约每秒一千次迭代。
逐周期执行的快环只包含读计数器、water-filling、虚拟队列更新与遥测
发送；流表 dump（毫秒级耗时，仅供类内细分与回退）放入每 200 ms 一次
的慢环，内联在主循环里按周期数触发——放后台线程反而会因解释器锁
抖动快环的采样节奏。整条快环在 32 个流集合下的单次开销约 0.5 ms
（合每流集合约 14 µs，随流集合数近线性），在周期预算之内。响应律
的全部增益按墙钟时间定义、按 $T$ 离散化（§3.5），因此参数表与周期
的选择正交。

**遥测通道。** 每周期每流集合一条定长二进制记录（头部加定长结构体，
含 $s, r, e, \hat e$ 与序号）。定长编码在毫秒级周期下比文本编码便宜
一个数量级。记录走接收端 DPU 到发送端 DPU 的带内路径（两块 DPU 的
PF SF 挂在同一下行桥上），稳态往返约 0.1 ms，不依赖专用管理网。

**驱动 RDMA 执行器。** 发送端 agent 与 NIC 拥塞控制引擎之间的 mailbox
一次调用阻塞约 13 ms，因此预算写入与律解耦：律每周期更新每流集合的
预算快照，一个合并写进程按 mailbox 能吸收的节奏把最新快照推下去。
这样政策目标的传播延迟被限制在约一次 mailbox 往返，而拥塞响应本身
不受影响——引擎的 per-QP 内环跑在事件速率（亚微秒）上，从不等待
mailbox。预算推送还做两处整形，共同点是让引擎面对一个准静态的目标：
预算漂移小于一个小比例时重发旧值（引擎把任何预算变化都当作上限阶跃
处理，会重置其内部的稳定保持窗；滞回使积分环节得以收敛），以及限制
预算的下降斜率（上升不限——恢复越快越好）。引擎内部把预算按 per-QP
的速率档施加到每条 QP，其防塌陷地板作用在测得的聚合速率上而非 per-QP
档上，因此与 QP 数无关。

**驱动 TCP 执行器。** pace shim 把每对（源，目的）的 pace 写入主机
BPF map；VF egress 上的 clsact 程序按 map 给报文盖 EDT 时间戳，fq
qdisc 兑现。shim 启动时为全部本地—远端 VM 对播种状态，保证执行面的
覆盖随控制面自动生长。

**参数。** 表 2 给出全部缺省值；时间量一律以墙钟语义标注。参数敏感性
的系统扫描见 §5：缺省点位于宽平坦区中央，仅加性增益与测量窗宽两个
方向存在失稳边界。

| 参数 | 缺省值 | 含义 |
|---|---|---|
| $T$ | 1 ms | 控制周期 |
| headroom | 3% | 分配总额低于物理容量的余量 |
| $\delta$ | 15% | 需求估计的增长余量 |
| $A$ | 线速的 1% / s | 加性增速率（HAI 最高放大 8 倍） |
| $\beta,\ \tau$ | 0.3, 50 ms | 乘性减剂量与时间常数 |
| $V$ | 1.2 Gbit | 标记满刻度（固定比特量） |
| $\theta_p,\ g_p$ | 5%, 2 | 探测余量比例与增益 |
| MD 下界 | 0.3 | 公平上限（与测得速率）的比例地板 |
| app-limited | 85%, 0.5 s | 判别阈值与窗口 |
| fail-open | 0.25 s / 2 s | 冻结 / 斜坡回升的超时 |
| 速率窗 | 20 ms | 固定样本数窗的目标宽度 |
| 预算滞回 / 斜率 | 3%, 0.35/s | RDMA 预算整形 |

**footprint。** 两端 agent 各占用一个 Arm 核；主机侧导出器与 shim
的开销可忽略。控制状态与流集合数同阶，与 QP 数无关：数据面维持数千
QP 时，控制环的周期与逐流集合开销不变（§5）。

---

# English Version

## 3 Design

### 3.1 Overview

HPFT provides two layers of fairness at the network edge: hard isolation
between tenants according to what they purchased, and weighted,
work-conserving sharing between a tenant's TCP and RDMA traffic. Two
practical constraints shape the design. Tenant stacks cannot be modified,
and switches cannot be expected to provide per-tenant queues or custom
scheduling. Everything must therefore happen inside the infrastructure
domain — on the DPUs at the two ends of a connection — and must steer both
transports with signals they already understand.

The starting observation is that the bottlenecks an operator cares most
about produce no congestion signal at all. When a VM that purchased
20 Gb/s sends over an idle 100 Gb/s link, nothing queues, nothing is
dropped, and nothing is marked: the scarcity is contractual, not physical.
Where physical signals do exist — at an incast, say — TCP and lossy RDMA
read the same signal differently: DCQCN backs off within milliseconds of
an ECN mark, while Cubic waits for an actual loss. Either way the outcome
is the same: who gets how much is an accident of colliding congestion
control algorithms, unrelated to anyone's policy.

HPFT answers both problems with a single move: **the owner of each
resource manufactures the signal itself.** Every controlled resource has
a natural owner — the uplink belongs to the sending host; the destination
VM's purchased share and the downlink belong to the receiving host — and
whoever owns a resource decides, by policy, how it is divided. The owner
keeps a ledger for its resource on its own DPU. Every control period it
divides the resource's capacity among the current competitors into
per-competitor entitlements, and for each competitor it maintains a
*virtual queue* — no packets ever wait in it; it merely integrates over
time the excess of the arrival rate over the entitlement — whose
normalized length is returned to the sender as a mark. The virtual queue
as a building block goes back to HULL's phantom queue, but there it lived
in the switch and existed to create bandwidth headroom; here it lives at
the edge and carries a hierarchical tenant policy.

On the sending side there is exactly one response law, and all traffic —
regardless of tenant or class — runs it: increase gently on unmarked
feedback, back off proportionally on marks. The law deliberately carries
no weights; all differentiation happens where the marks are minted.
Because weights live only in the owner's scheduler, changing policy means
changing one ledger, and senders remain oblivious. And because the law is
identical everywhere, a flow that crosses several controlled resources
needs no coordination among their owners: each owner marks according to
its own ledger, the sender responds to whichever signal is currently
firing, and the rate settles at the smallest of the per-resource
entitlements. This matters more than it may seem: weights normalized
within one owner's ledger are simply not comparable across owners, and in
this architecture they never need to be compared.

Both kinds of heterogeneity then dissolve in front of the same mechanism.
Policy bottlenecks and physical bottlenecks become indistinguishable to
senders, because the ledger synthesizes contractual scarcity into the
same signal as physical scarcity. TCP and RDMA become indistinguishable
to the marking, because the law responds to marks, not to transports.
Even inter-class borrowing needs no mechanism of its own: when one class
goes idle, the work-conserving division of the ledger hands its share to
the sibling class, which grows into it unmarked; when demand returns, the
shares are redrawn and marks push the borrower back.

[Figure 1: The HPFT control loop. Each period, the receiver DPU measures
per-flow-set arrival rates r_f, computes entitlements e_f and fair
ceilings ê_f by hierarchical water-filling, updates the virtual queues to
produce marks s_f, and returns them in telemetry; the sender DPU runs the
response law to maintain a rate cap R_f per flow-set and installs
pace_f = min(R_f, Tree_f) into the enforcers; the tenant's own congestion
control keeps running underneath the pace.]

Figure 1 shows one turn of the loop. Each period the receiver does three
things: it measures the arrival rate of every flow-set, computes what
each is entitled to right now by hierarchical water-filling, and
integrates any sustained excess into marks that travel back (§3.3, §3.4).
The sender updates its rate cap from the telemetry and installs the
minimum of that cap and its local policy-tree share into the enforcers
(§3.5, §3.6). The tenant's congestion control always runs below the pace:
the actual sending rate is the smaller of what the tenant wants and what
the pace allows, and bursts shorter than a control period are absorbed by
the tenant's own control loop.

### 3.2 Service Model and Policy Semantics

**Deployment model.** Every host carries a DPU. Tenant VMs send and
receive through SR-IOV virtual functions (VFs), and all traffic traverses
the DPU's eSwitch. The DPU belongs to the infrastructure domain; tenants
can neither see nor modify it. Each VF has a unique MAC address, so on
the DPU a (source MAC, destination MAC) pair uniquely identifies a
(source VM, destination VM) pair.

**Flow-sets.** The unit of observation and rate control is the
*flow-set*: the triple {source VM, destination VM, class}, where the
class is TCP or RDMA. All RDMA traffic from VM1 to VM5 is one flow-set;
the system never subdivides down to individual flows or queue pairs.
Policy is expressed over flow-sets and control state is kept per
flow-set, so control-plane cost grows with the number of flow-sets and is
independent of the number of QPs.

**Policy parameters.** Operator and tenant intent reduces to four kinds
of parameters (Table 1), each with a single owner and a single point of
effect.

| Parameter | Meaning | Set by | Takes effect at |
|---|---|---|---|
| $W_i$ | inter-tenant weight of tenant $i$ | operator | VM layer of the owner's scheduler |
| $MaxRate_i$ | purchased bandwidth cap of tenant $i$ | operator | sender tree (egress cap) and receiver scheduler VM layer (ingress cap) |
| $W_{i,c}$ | class weights of tenant $i$ | tenant $i$ | owner side: sender tree uses the source VM's, receiver scheduler the destination VM's |
| $u_{d,c,s}$ | per-sender weight of destination $d$ for source $s$ in class $c$ | tenant $d$ | flow-set layer of the receiver scheduler (defaults to 1) |

Fairness is defined over link (wire) bytes — wire bytes are what
$MaxRate$ sells.

**Scope of the guarantee.** HPFT gives steady-state guarantees at *edge
bottlenecks*: allocations converge to the hierarchical weighted max-min
shares defined by policy. An edge bottleneck is one with a clear owner
who can see all competing traffic on it, and there are three: the
sender's uplink (everything leaves from this host), the destination VM's
policy share (everything lands on this host), and the receiver's downlink
(incast — the one place an N-to-1 rate mismatch materializes is the last
hop toward this host, and what the host receives never exceeds line rate,
so the receiver sees all of it).

Two boundary clarifications are in order. First, class weights are
work-conserving targets: if a class's own congestion control cannot fill
its weighted share on a physically congested link, the difference is lent
to the sibling class rather than left idle — a direct consequence of the
borrowing semantics. An operator who wants weights enforced strictly even
under physical congestion can disable borrowing for a class, at a
utilization cost. Second, explicitly out of scope are core-network
bottlenecks (no owner can see all traffic there; tenant congestion
control copes as it does today), loss elimination, path load balancing,
and burst precision finer than one control period, which the tenant's own
control loop absorbs — DCQCN's CNP response is sub-millisecond and always
runs below the pace.

### 3.3 Receiver: Measurement and Entitlements

The receiver's first job is to know, accurately, who is arriving and how
fast. The measurement must be both fresh — one usable reading per control
period — and split by class, and these two properties are exactly the
ones that the available hardware counters each lack half of. The offload
flow-table counters, refined to (MAC pair, class) granularity, split by
class but are refreshed by the driver far too slowly for per-period
control; the per-VF port counters are fresh at any frequency but count
only totals. What bridges them is a simple fact: RDMA bypasses the
kernel, so the host kernel's byte counters see only TCP. A lightweight
exporter on the receiving host therefore streams each VF's kernel TCP
byte counts to the DPU, and every period the DPU subtracts the TCP rate
from the fresh port total to obtain the RDMA rate — both class rates are
now fresh. The slow flow-table counters are relegated to splitting a
class among multiple senders, and serve as a fallback if the exporter
fails (§4).

Given each flow-set's arrival rate $r_f$, the receiver first estimates
demand:

$$D_f = r_f\,(1+\delta).$$

The small margin $\delta$ exists to unlock suppressed flow-sets: one that
is currently held down has a small $r_f$, and taking $r_f$ itself as
demand would freeze the allocation at that low point. The margin gives it
room to grow; as it grows, next period's demand grows with it, until it
reaches its fair share. The margin must stay small: the water-filling
ledger is zero-sum, and share granted to a recovering flow-set that it
cannot yet use is taken precisely from what its siblings could have used.
Acceleration of recovery belongs on the sender (fast recovery, §3.5), not
on the demand side.

Then comes a three-layer capped water-filling — pour capacity in
proportion to weights, satisfy small demands first, and let the remainder
keep flowing to the unsatisfied:

- **VM layer:** pour the root capacity $C_{root}$ (the downlink line rate
  minus a small headroom) across destination VMs in proportion to $W_d$,
  capping each VM at $\min(MaxRate_d,\ \text{sum of demands below it})$;
  this yields $C_d$.
- **Class layer:** pour $C_d$ across {TCP, RDMA} in proportion to
  $W_{d,c}$. When one class demands less than its weighted share, the
  remainder goes to the other class — borrowing arises here, with no
  explicit protocol; this yields $C_{d,c}$.
- **Flow-set layer:** pour $C_{d,c}$ across sources in proportion to
  $u_{d,c,s}$; this yields each flow-set's entitlement $e_f$.

The headroom deliberately keeps the allocation total below physical
capacity, so that the virtual queues alarm before the switch's physical
queue does.

The scheduler computes one more quantity, the *fair ceiling* $\hat e_f$:
the allocation this flow-set would receive from the same tree if its own
demand were infinite while everyone else's stayed real. Always
$\hat e_f \ge e_f$. The two differ in one essential respect: $e_f$ is
demand-capped and thus depends, through $D_f$, on the flow-set's own
$r_f$; $\hat e_f$ is independent of the flow-set's own behavior and is
determined only by policy and by others' demands. Wherever a reference
value must not be dragged down by the flow's own current state — the
drain of the virtual queue (§3.4) and the upper bound on probing (§3.5) —
$\hat e_f$ is the quantity used. The fair ceilings of all flow-sets are
computed in one pass by a cascade algorithm in $O(N\log N)$, exactly
equivalent to recomputing the tree per flow-set (§4).

### 3.4 Receiver: Virtual-Queue Marking and Telemetry

Each flow-set has one integrator, charged by excess and drained by the
unused fair ceiling:

$$vq_f \leftarrow \mathrm{clip}\!\big(vq_f + \Delta_f\, T,\ 0,\ V\big),
\qquad
\Delta_f = \begin{cases}
r_f - e_f, & r_f > e_f,\\[2pt]
-(\hat e_f - r_f), & r_f \le e_f,
\end{cases}$$

and the mark is the normalized queue length: $s_f = \min(vq_f/V,\ 1)$,
where $V$ is the full scale. Note that the virtual queue accumulates a
rate integrated over time, so $V$ is a fixed quantity of bits,
independent of the control period. Integrating, rather than comparing
instantaneous ratios, makes the mark tolerant of single-period
measurement noise and responsive only to sustained excess — the AVQ
lineage.

That the charge and the drain use different reference values is the one
piece of this mechanism most worth explaining. The symmetric choice —
draining by $e_f - r_f$ when below entitlement — hides a trap: $e_f$ is
demand-capped and therefore tracks $r_f$, so no matter how deeply a
flow-set has been suppressed, its drain rate is only a $\delta$-sized
fraction of its actual rate. A full queue then takes minutes to drain at
low rates, the mark stays saturated throughout, and the flow is pinned to
the floor indefinitely. Draining by the fair ceiling removes the trap:
$\hat e_f$ does not collapse with the flow's own rate, so a suppressed
flow-set has a large unused share $\hat e_f - r_f$, its queue drains
quickly, the mark clears, and the flow recovers — while a flow-set
sitting exactly at its share ($r_f \approx \hat e_f$) drains at a
vanishing rate and gets no free absolution. A fully idle flow-set
($r_f = 0$) neither charges nor drains, so its queue is simply cleared
within a period: a flow that has stopped sending abandons its congestion
claim, and stale state should not keep punishing it when it resumes.

This one mechanism unifies three scenarios that look nothing alike.
Policy contention: physically there is no congestion, but the $MaxRate$
cap at the VM layer makes the over-quota flow-sets show $r_f > e_f$ — the
ledger synthesizes the contractual scarcity. Incast: the root capacity is
oversubscribed, the shortfall is spread into every $e_f$ in proportion to
the weights, and the headroom guarantees the virtual queues alarm before
the physical queue. Borrowing reclaim: the idle class's demand returns,
shares are redrawn, the borrower's $e_f$ moves down and its marks rise.
In front of the marks, the three scenarios are the same scenario.

On a single controlled resource this is the classical setting of an AQM
with differential marking plus AIMD sources: flow-sets above $e_f$ are
persistently marked and multiplicatively decreased, those below are
unmarked and additively increased, and the equilibrium sits at
$r_f \approx e_f$, which water-filling made equal to the policy share.
HPFT's law is layered above the tenant's own congestion control, and the
two loops do not fight: the pace delays packets but never drops them, so
to tenant TCP it looks like a smooth pipe; on the RDMA side the enforcer
semantics are exactly min(DCQCN, budget), with the tenant loop always
responding faster than, and below, the pace.

**Telemetry.** Every period, the receiver emits one fixed-size record per
present flow-set: $\{f,\ s_f,\ r_f,\ e_f,\ \hat e_f\}$. Two semantics
deserve emphasis. First, $s_f = 0$ is transmitted, not implied: an
explicit zero mark is a fresh permission — "no congestion, verified" —
and the sender's additive increase is conditioned on receiving it, so
absence never masquerades as permission. Second, presence, not traffic,
drives emission: as long as a flow-set exists (its forwarding entries are
present), permissions keep flowing even in a period when its measured
rate happens to read zero. Were telemetry emitted only for $r_f > 0$, one
transient zero reading would push the sender into fail-open, ramp it to
its local cap, and have it beaten back down when traffic resumes — a
cycle rather than a recovery. The sender's law needs $e_f$ and
$\hat e_f$ as well (§3.5); carrying them alongside the mark makes
telemetry the law's single input channel.

### 3.5 Sender: the Response Law

The sender maintains one rate cap $R_f$ per flow-set, updated on every
telemetry record, and installs $pace_f = \min(R_f,\ Tree_f)$. The cap
starts at $Tree_f$: a new flow-set immediately receives everything the
sender-side policy allows, with no slow start — if it lands on a
congested resource, marks arrive within a period or two and the
multiplicative decrease pulls it down, the transient is absorbed by the
tenant's own congestion control, and it is bounded by the sold quota
throughout (§3.6).

The skeleton is weightless AIMD: increase on zero marks, decrease
multiplicatively on marks. Around that skeleton, each branch has a shape
worth explaining.

**Probing is anchored at the fair ceiling.** A long run of zero marks
means the flow-set has been sitting below its available share, so the
additive step escalates gradually (the Hyper Increase structure borrowed
from DCQCN, with a cap). But probing must be bounded: without a bound,
one transient dip during a sole-occupancy period lets the escalated probe
push $R_f$ far past the allocation, after which the queue saturates,
marks storm, the law cuts deep, and the cycle repeats as a slow
relaxation oscillation. The bound is anchored at the fair ceiling:

$$R_f \le \hat e_f + \min\!\big(\theta_p\,\hat e_f,\ \
g_p \cdot \max(0,\ \hat e_f - r_f)\big),$$

with a small proportional margin $\theta_p$ and a margin gain $g_p$. The
shape of this expression follows from what the margin is for: the only
reason to allow $R_f$ above $\hat e_f$ at all is that an enforcer may
under-realize its pace — if the pace is set to $\hat e_f$ but only ninety
percent of it shows up on the wire, the probe must be allowed slightly
higher for the share to actually be consumed. So the margin decays to
zero as $r_f \to \hat e_f$: an under-realizing enforcer receives
compensation proportional to its gap, while a perfectly realizing one
converges to exactly $\hat e_f$ and never manufactures the sustained
excess that would slowly fill the virtual queue. The anchor must be
$\hat e_f$, not $e_f$: the latter depends on the sender's own $r_f$
through the demand estimate, so any slack in the enforcement drags the
fixed point toward zero — the same reasoning as for the drain reference
in §3.4.

**The downward dose is defined per wall-clock time.** If marks persist
for several periods, the sender decreases repeatedly. So that the total
reduction over a fixed stretch of wall-clock time does not depend on the
choice of period, the decrease is defined as an exponential decay in
time, discretized by the period:

$$R_f \leftarrow R_f\,(1-\beta\, s_f)^{\,T/\tau},$$

with $\tau$ the law's decay time constant. The decrease is also floored:
$R_f$ is never cut below a fixed fraction of the flow-set's fair ceiling
(nor of its currently measured rate). Class entry and exit produce brief
mark storms during the transition, and without a floor, back-to-back
decreases would push the pace to depths from which hardware enforcers
recover slowly; the fair ceiling is a policy quantity that does not
collapse with the measurement, so the floor holds no matter how long the
marks persist.

**Fast recovery returns the flow to within its share — and no further.**
After a deep cut, additive increase alone climbs back too slowly. The
sender remembers $R_{good}$, the value held just before the most recent
cut; from then on, each zero-mark record moves the cap halfway back,
$R_f \leftarrow (R_f + R_{good})/2$, converging within a few periods. If
the mark was measurement noise, the rate snaps back; if the constraint
was real, the next decrease overwrites $R_{good}$ with a lower pre-cut
value, and the recovery target walks itself down instead of fighting the
constraint. The rebound target is additionally capped by the same probe
bound as above: the receiver's $e_f$ lags by a period or two, and a
recovery that jumps far above the current share is immediately in excess,
immediately marked, and cut again — locking the flow low. The principle
mirrors probing: fast recovery sends a flow back into its share; anything
beyond that is left to the slow additive increase.

**App-limited flow-sets are frozen near their actual rate.** If a
flow-set's measured rate stays below a fraction of its share for a while,
the application itself is not using the allocation; $R_f$ is frozen just
above $r_f$ and additive increase stops until $r_f$ climbs back. An
inflated cap has no value and only takes extra rounds of decrease to work
off when congestion arrives. The reference for "using its share" is
$\min(R_f, e_f)$, not $R_f$ itself: a flow-set that fills its share but
is merely capped is not app-limited and must not be clamped.

**Fail-open on telemetry loss.** If no telemetry arrives for a flow-set
over a short interval, the sender freezes $R_f$ (no more increase); if
the silence persists longer, it ramps $R_f$ up to $Tree_f$. The
semantics: when the receiver fails, traffic degrades to being governed by
sender-local policy alone — tenants are neither stalled nor unleashed,
because their own congestion control remains and $Tree_f$ is bounded by
the sold quota.

### 3.6 Sender: the Local Tree and Enforcement

The uplink is the sender's own resource and the owner sees every
competitor on it, so no marking loop is needed — it is scheduled locally.
One selling constraint governs: $MaxRate_i$ is what tenant $i$ bought,
and no VM may ever exceed its own purchase; when a VM underuses, the
spare capacity lets other VMs take more *within their own caps*
(oversubscription), never beyond them. For example, a 100 Gb/s uplink
oversold to eight 20 Gb/s VMs gives each 12.5 when all are active; if one
uses only 5, the other seven get about 13.6 each — still under 20.

This is realized in two layers. The first is a per-VM hardware rate
limiter — class-blind, static — whose job is to backstop the software
layer if it fails. The second is a software tree: the same water-filling
code as the receiver's, run over the uplink capacity, with the VM layer
capped at $\min(MaxRate_i,\ \text{sum of demands})$ — the selling
constraint is enforced by exactly this cap — and classes weighted inside
each VM. Each flow-set's $Tree_f$ is its fair ceiling on this tree:
$R_f$ starts at $Tree_f$ and fail-open ramps back to it, and both uses
require "the allowance policy grants", not a demand-capped share — a new
flow-set starting at $r = 0$ would otherwise be granted a near-zero
$Tree_f$, a contradiction. Inter-class borrowing on the sender side is
simply the work-conserving fill of this tree, consistent with the
receiver.

The final $pace_f = \min(R_f,\ Tree_f)$ goes to two enforcers. RDMA
flow-sets become per-flow-set budgets in the NIC's programmable
congestion-control engine, enforced by hardware pacing, invisible to the
tenant. TCP flow-sets become host-side pacing rates (an fq qdisc with
earliest-departure-time stamps). Under both enforcers the tenant's own
congestion control keeps running: the actual sending rate is
min(what the tenant wants, $pace_f$).

## 4 Implementation

**Components.** The control plane is about 1,800 lines of Python: the
receiver agent (832 lines, managed by systemd on the DPU's Arm cores)
implements measurement, scheduling, marking, and telemetry; the sender
agent (675 lines) implements the response law, the sender tree, and
enforcement; a rate exporter on the receiving host (88 lines) and a pace
shim on the sending host (116 lines) are two lightweight helpers. The
single-pass fair-ceiling algorithm (fastfill, 73 lines) is shared by both
ends and was validated for exact equivalence against per-flow-set
recomputation of the water-filling tree on 12,600 randomized cases. The
TCP enforcer is roughly 250 lines of BPF C (clsact + fq with EDT
timestamps). The RDMA enforcer reuses the NIC's programmable
congestion-control engine, which runs on the DPU's datapath accelerator;
HPFT's changes to it are confined to the budget interface and one floor
guard. All policy and parameters live in a single JSON registry that both
agents hot-reload on modification, so policy changes restart nothing.

**Measurement path.** Per-class rates are assembled from two hardware
counters (§3.3). The per-VF representor port counters update at
sub-millisecond granularity but are class-blind; the offloaded flow-table
counters, refined to (MAC pair, class) granularity by three
non-forwarding classification rules on the downlink bridge, are
class-resolved but batch-refreshed by the driver at second granularity,
usable only for slow purposes. The class split rests on the fact that
RDMA bypasses the kernel path: the host exporter pushes each VF's kernel
TCP byte count to the DPU every 5 ms, stamped with the host clock at the
moment of reading (the two are captured together to stay aligned), and
each period the DPU subtracts the TCP rate from the port total to get
RDMA. All rates are computed over windows holding a fixed number of
samples (about 20 ms wide) rather than a fixed time span — a fixed time
window is fragile against loop jitter: if a period runs slightly late,
the only old sample ages out of the window, producing a divide-by-zero
followed by a double-length interval; a fixed sample count also smooths
both counter-update quantization and the millisecond-scale burst
structure of the transports. The flow-table counters are relegated to
splitting a class among senders; if the exporter's reports stop arriving,
the agent falls back to flow-table attribution, so measurement survives
the failure of its auxiliary channel.

**Control loop.** The control period is $T$ = 1 ms; the receiver's main
loop sustains about a thousand iterations per second. The per-period fast
path contains only counter reads, water-filling, virtual-queue updates,
and telemetry emission; the flow-table dump (milliseconds of work, needed
only for intra-class splits and fallback) runs on a 200 ms slow path,
triggered inline in the main loop by period count — moving it to a
background thread would, under the interpreter lock, jitter the fast
path's sampling cadence instead of isolating it. The whole fast path
costs about 0.5 ms per tick at 32 flow-sets (about 14 µs per flow-set,
near-linear in the number of flow-sets), within the period budget. All
gains of the response law are defined per wall-clock time and discretized
by $T$ (§3.5), so the parameter table is orthogonal to the choice of
period.

**Telemetry channel.** One fixed-size binary record per flow-set per
period (a header plus a fixed struct carrying $s, r, e, \hat e$ and a
sequence number); fixed-size encoding is an order of magnitude cheaper
than text at millisecond periods. Records travel an in-band path from
receiver DPU to sender DPU (the PF SFs of both DPUs sit on the same
downlink bridge), with a steady-state round trip of about 0.1 ms and no
dependence on a dedicated management network.

**Driving the RDMA enforcer.** A single mailbox call into the NIC's
congestion-control engine blocks for about 13 ms, so budget writes are
decoupled from the law: the law refreshes each flow-set's budget snapshot
every period, and a coalescing writer pushes the latest snapshot at
whatever cadence the mailbox absorbs. Policy-target propagation is thus
bounded by about one mailbox round trip, while congestion response is
unaffected — the engine's per-QP inner loop runs at event rate
(sub-microsecond) and never waits for the mailbox. The budget stream is
additionally shaped in two ways, both serving the same end of presenting
the engine with a quasi-static target: budgets drifting by less than a
small fraction are re-sent unchanged (the engine treats any budget change
as a cap step and re-arms its internal settle-hold; hysteresis lets its
integrator converge), and the budget's downward slew rate is limited
(upward is unrestricted — recovery cannot be too fast). Inside the
engine, the budget is applied as per-QP rate levels; its anti-collapse
floor acts on the measured aggregate rate rather than per QP, making it
independent of the number of QPs.

**Driving the TCP enforcer.** The pace shim writes each (source,
destination) pair's pace into a host BPF map; a clsact program on each
VF's egress stamps packets with earliest-departure times from the map,
and the fq qdisc realizes them. The shim seeds state for all
local-to-remote VM pairs at startup, so enforcement coverage grows
automatically with the control plane.

**Parameters.** Table 2 lists all defaults; time-valued entries are given
in wall-clock terms. A systematic sensitivity sweep appears in §5: the
default point sits in the middle of a wide plateau, with instability
cliffs only in the directions of an over-large additive gain and an
over-wide measurement window.

| Parameter | Default | Meaning |
|---|---|---|
| $T$ | 1 ms | control period |
| headroom | 3% | allocation total kept below physical capacity |
| $\delta$ | 15% | growth margin of the demand estimate |
| $A$ | 1% of line rate / s | additive-increase rate (escalation up to 8×) |
| $\beta,\ \tau$ | 0.3, 50 ms | multiplicative-decrease dose and time constant |
| $V$ | 1.2 Gbit | marking full scale (a fixed bit quantity) |
| $\theta_p,\ g_p$ | 5%, 2 | probe margin fraction and gain |
| MD floor | 0.3 | floor as a fraction of the fair ceiling (and of the measured rate) |
| app-limited | 85%, 0.5 s | detection threshold and window |
| fail-open | 0.25 s / 2 s | freeze / ramp timeouts |
| rate window | 20 ms | target width of the fixed-sample-count window |
| budget hysteresis / slew | 3%, 0.35/s | RDMA budget shaping |

**Footprint.** Each agent occupies one Arm core; the host-side exporter
and shim are negligible. Control state scales with the number of
flow-sets, not QPs: with the data plane sustaining thousands of QPs, the
loop period and per-flow-set cost are unchanged (§5).
