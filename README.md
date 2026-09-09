# HPFT — implementation repo

HPFT（HyperFront）在 BlueField-3 DPU 的边缘上让共享同一个接收端口的云租户按政策分到带宽：
租户之间按权重、租户内 TCP 与 RDMA 两类按类权重、类内按发送方，三层份额；没用完的份额可以借、
一秒内要得回；不依赖也不修改租户自己的拥塞控制。租户不改代码、数据面不改协议。
本仓库是**实现**：DPU 与 host 上的 agent、执行面、lab 编排、工程验证数据。

**本文描述系统现在的样子。** 没有变更史——过往的实现、调试过程、设计变动
都在 git 里（`git log -p <文件>`），需要时再查。

## 先读什么

新接手一个任务时，按下面的顺序读，读到够用为止：

| 想知道什么 | 读哪里 |
|---|---|
| 系统是什么、每个部件解决什么问题、为什么这样设计 | `hpft-design/docs/design_v4.md`（唯一权威设计说明；英文版 `design_v4_en.md`） |
| lab 长什么样、怎么跑一场实验 | **本文**的"现在的系统"与"怎么跑实验" |
| 动 lab 之前不能不知道的坑 | 本文"硬性规则" + `hpft-design/docs/ops_notes.md` |
| 参数为什么取这个值、换规模怎么重新定 | `hpft-design/docs/design_theory_v4.md` |
| 设计的每一条落在哪个文件、实现与设计的已知差异、开关 | `docs/IMPLEMENTATION.md`（随实现更新） |
| 本平台量出来的数字与环境事实 | `hpft-design/docs/platform_notes.md` |
| 论文评估要跑哪些实验 | `hpft-paper/paper/evaluation_plan.md` |

改代码之前**不需要**通读设计文档：`config/lab-registry.json` 是唯一真值配置，
`tools/lab-infra/deploy_check.sh` 会告诉你四台机器和 repo 是否一致。

三个仓库是同一份 git 历史用 `git filter-repo` 切出来的，**必须放在同一个父
目录下作兄弟目录**（`~/hyperfront/{hpft-implementation,hpft-design,hpft-paper}`）——
大量脚本按绝对路径引用同级的 `~/hyperfront/perftest-enhanced`、`~/hyperfront/bfb`。

| 仓库 | 内容 |
|---|---|
| **hpft-implementation**（本仓库） | `tools/` `tcp/` `config/` `validation/`，工程验证数据 `results/` |
| `hpft-design` | 设计说明、理论、操作规程 |
| `hpft-paper` | 论文草稿、motivation 实验包、评估计划与数据 |

## 现在的系统

### lab

四台机器 `sgpu01`–`sgpu04`，每台一块 BlueField-3 挂在同一台 SN5600 上，
四个 p1 口统一 **200G**（交换机侧 swp37s1 / swp37s0 / swp3s1 / swp4s1）。
sgpu01/sgpu02 用 PCI `38:00` 那块卡，sgpu03/sgpu04 用 `b8:00`（host netdev
`bf1_1`）。每台 **8 个 VF**：host 侧 `dpu1vf0-7` = `10.1.i.<机器号>`，DPU 侧
representor `pf1vf0-7`。每个 VF 卖 50G，两层限速都在 VF 自己的 DPU 上：上行 devlink `tx_max`、下行 OVS drop-band meter（`tools/lab-infra/vf_caps.sh sync`；fw reset 后必须重做；验证见 `results/vf_caps_20260826/`）。

数据面是常驻 VxLAN overlay，**静态全网状、带水平分割**：`ovsbr-p1` 挂 8 个
representor 加 3 条 vxlan 隧道（每台到其它三台各一条；`tos=inherit` 是硬性要求，否则 ECN 位过不了
封装），p1 直接带 underlay `172.16.1.x` 与遥测 `10.1.9.x`、MTU 9000。
转发不靠 MAC 学习和泛洪，全部由注册表生成显式规则：本地 VF 发往远端的包按目的 IP 进对应隧道，
隧道进来的包按目的 IP 打 meter 后直接送到本地 VF 的 representor，隧道进来的其它一切丢弃——
从隧道进来的永远不再出隧道，所以没有环路；发往远端的包从不泛洪，所以 MAC 不会学错。
`tools/lab-infra/overlay.sh` 是它的唯一实现（头注释有规则表与两个坑）。

**角色是每场实验的决定，不是节点的属性**：四台 DPU 装的是同一份载荷（收发
两侧都有）。常设形态是 `roles.sh all`：每台都同时跑接收端与发送端两套代理，谁收谁发由打流表决定。
交换机 sn5600 常设是一台；只有 V8 临时用回环线切成两台（`tools/lab-infra/switch/README.md`）。

### 三个面

配置来自 `config/lab-registry.json`，**repo 副本是唯一真值**；改政策先改 repo，
再用 `deploy_check.sh --deploy` 下发到各 DPU 的 `/opt/hpft/`，接收端按 mtime
热加载 policy 段。

- **接收端 DPU** 跑 `hpft-rxagent-e`（`tools/dpu/rx_agent.py`）：vport 硬件计数器直读得到每个
  目的 VM 每类的到达量、按发送端自报拆到流集合 → 物理容量（端口减不调度流量再乘 $1-h$）→
  两次三层填平分配（VM 上限 → 类 → 发送方；第一次得份额，第二次给出借方有限需求得应得 $E$）→
  虚拟队列 → 每周期把归一化的队列 $q$ 发给发送端 DPU。同机还有
  `hpft-vport-meter`（`tools/dpu/vport_meter.c`），1ms 直读 vport 计数器并按
  ib/eth 分出 RDMA 与 TCP 两类。
- **发送端 DPU** 跑 `hpft-txagent-e`（`tools/dpu/tx_agent_e.py`）：调速律得许可速率 $R$ →
  与本机上行链路树的上限 $U$ 取小 → 执行（RDMA 写 PCC mailbox；TCP 经 UDP 交给该 host 上的
  `hpft-pace-shim` 写 BPF map）；同时把本机每个 VF 每类的发送速率报给所有其它节点，供接收端拆分。
  同 host 还要跑 `hpft-qpn-resolver`，它把 `{qpn → 流对}` 喂给代理再到执行面；**没有它执行面不知道
  哪些 QP 属于同一个流集合，每个 QP 都按未知流集合的放行额度跑**。
- **执行面只读租户 CC、不改它：每个流集合一个令牌池**（设计 6 节）。池按许可速率 $R$ 续满，
  流集合里的每个 QP 或每条 TCP 连接按自己的 CC 速率 $c_i$ 取令牌，每条流的上限是
  $r_i=\min(c_i,(R+P)/N)$，$P$ 是池上一个四分之一毫秒攒下的存量、$N$ 是最近一毫秒在发的流数。
  RDMA 侧是 `tools/dpu/pcc/rp_rtt_template_dev_main.c`
  （DOCA PCC device 码，跑在 DPA 上）：四张卡都设了 `ROCE_CC_SHAPER_COALESCE_P2=SOURCE_QP`，
  一个 QP 就是一条 PCC 流，租户 CC（DCQCN 缺省、Swift、ZTR）逐 QP 运行、事件准确归属；QP 记录
  以（vhca_id, QPN）为键。TCP 侧是 `tcp/bpf-opt3/hpft_tcp_edt_kern.c`：每条连接读自己的
  `snd_cwnd`，$r_i = R\,w_i/\sum w_j$，有自己的 EDT 时钟与欠账上限，分母每 10 ms 由真的发过包的
  连接重建。对照臂在设备码里：`CC_ONLY=1` 只跑租户 CC，`LAW=1` 等额封顶（没有池），`LAW=2` 忽略 CC 等分。

### 响应律

接收端为每个流集合记一本虚拟队列 $Q\leftarrow\operatorname{clip}(Q+A-E,\ 0,\ D\cdot E)$，
**只把归一化的队列 $q=Q/E$ 发出去**，线上没有任何能被反算成速率的量。发送端每收到
一条反馈做一次乘性步进：账本空着时 $R\leftarrow R(1+\alpha m)$，$m$ 是连续空账的反馈数（封顶 $m_{\max}$）；
随后总是 $R\leftarrow R\big(1-\operatorname{clip}(\kappa\Delta q+\tfrac{\kappa}{D}q,\ \pm\tfrac12)\big)$，
账本在涨时刹车、有存量时还账。$\Delta q$ 由发送端自己从相邻两条反馈相减得到，所以一个标量同时带了误差和它的积分。
上探不越过 $R_{\max}=\min(\max(R_0,2\bar A_s),U(1+\delta))$；新流集合从 $R_0=hC$ 起步。
`e_params.step_form=exp` 把同一个指数写成 $e^{x}$，只作对照。
遥测**rx/tx 是双端同步的二进制格式，必须一起下发**。控制周期 10 ms，现行
`kappa=0.1`、`D=30`、`alpha=3e-4`、`m_max=100`。

这些常数全是每条反馈的无量纲量，**不随链路速率或流数改变**；换平台只需重测周期
$T_p$ 与环路滞后 $\tau$，其余是代数。推导、执行面的四条性质与调参配方见
`hpft-design/docs/design_theory_v4.md`，设计本身见 `design_v4.md`。

### 离线检查（不需要 lab，直接跑）

`tools/tests/` 下四组：`rx_check.py`（接收端的拆分与发送端的下降限速）、`loop_dryrun.py`（tx_agent 对合成遥测跑
整环，含真 FIFO 与本地 UDP 假接收端）、`fastfill_test.py`（C 分配器与它的 Python 参照
对拍）、`hw_maxrate_test.py`（硬件层单位——devlink 入参 bit/s、读回 byte/s）。执行面的检验在 lab 上做：
`validation/` 的判据 6 读执行面自己的回读。

## 怎么跑实验

```bash
tools/reboot_recover.sh                       # 仅在 DPU 重启 / fw reset 之后
tools/lab_env.sh hpft                         # 环境：PCC+HPFT（另有 plain / jakiro / ztr / swift）
tools/lab-infra/roles.sh all                  # 四台都同时收发（2026-09-04 起常设）
tools/lab-infra/deploy_check.sh               # 必须通过，否则数据无意义
tools/lab-infra/flow_preflight.sh "0,0 1,1 2,2 3,3" sgpu03 sgpu02   # 每个发送端各验一次
bash validation/run/run.sh V1_incast <tag>    # 场景、判据与跑法在 validation/README.md
python3 validation/distill.py <tag>           # 六条判据
python3 validation/plot/timeline.py <tag> V1  # fig/V1_timeline.png
python3 validation/plot/executor.py <tag> V1  # fig/V1_executor.png
python3 validation/report.py <tag>            # reports/<tag>.md
```

整套连跑：`validation/run/campaign.sh <日期标签>`（V1–V7，再临时切双交换机跑 V8 两个臂），三个小时以上，跑之前先说一声。最近一轮的结果在 `validation/STATUS.md`。

每一步都幂等。`roles.sh` 与 `deploy_check.sh` 的节点集来自 registry 的 `nodes`；
`flow_preflight.sh`、`eval_lib.sh` 不带参数时退回
registry 的 `sender_host`/`receiver_host`。评估战役的 runner 用环境变量
`EVAL_RECEIVER` / `EVAL_SENDERS` 选节点。

**读结果**：`validation/distill.py` 按**类内**和**按目的 VM** 报公平性，而不是一个
扁平 Jain。类权重把每个目的 VM 在 TCP 与 RDMA 之间对半分，所以两类的发送端
数量不同时，发送端少的那一类每条流**本来就该**拿得多；扁平 Jain 会把正确的
策略行为报成不公平，只有当每个目的 VM 的类混合相同时才有意义。

## 硬性规则

每一条都对应一次"每一层都报健康、数据却是错的"的事故。

- **PCC 实现的 DCQCN 与 Swift 只有一份**：`tools/dpu/pcc/rp_rtt_template_dev_main.c` 里 `0xccd 2` 与
  `0xccd 3`，参数与定稿数字见该目录的 README。不要拿原厂 `rp_rtt_template` 模板
  二进制当基线：它们把 slot 15 的事件交给框架内置算法，跑出来的是固件内置 CC 的成绩。`lab_env.sh swift|ztr`
  起的是本执行面的 Swift 与 ZTR 单独运行。
- **四台主机之间登录用户必须能免密 ssh 到彼此**（`deploy_check.sh` 会查）。QPN 解析器以登录用户
  身份对每个对端 `rdma res show qp` 做配对，登不上就送不出任何绑定，那台机器的每个 QP 都按
  "未知流集合"的放行额度跑，而每一层服务都报 active。密钥不齐时的症状是某台发送端的
  流集合恰好卡在放行额度乘 QP 数附近。
- **标记多少看交换机，不看执行面的 CNP 计数**：接收网卡生成 CNP 有速率上限，每台发送端 12 s
  约 200 万就饱和。`validation/run/quick_sw.sh` 在一场快跑前后读 sn5600 接收端口的
  `ecn-marked-frames` 与队列丢帧。
- **改了 BPF 之后在每台主机跑 `tools/host/edt_reinstall.sh`**（先在 sgpu01 用 clang 编出 .o、
  `deploy_check --deploy` 分发）：它先拆掉每块 VF 上的旧 filter 再装新程序，装完核对 prog id 与
  `hpft_pair_state` 的长度，并重启 pace shim。
- **改过 PCC 设备码之后，`deploy_check --deploy` 重编完还要在四台 DPU 上 `bash /opt/hpft/rp_service.sh start`**：
  部署脚本只重编不重启，跑着的还是旧设备码。`validation/run/run.sh` 不重启执行面（一轮十几场之间不用重启），
  `quick.sh` 每场都重启。
- **执行面的 QP 记录以 (vhca_id, QPN) 为键、按键落位，不以 PCC 框架的每流上下文为键，也不从共享游标取号**：那份上下文不是一个 QP 一份
  （两个 VF 的两个 QP 会轮流出现在同一份里，QP 销毁后也不清零），DPA 上又没有原子操作。
  校验用 `0xded` 回读的在册数是否等于每流集合的 QP 数（`validation/data/<tag>_executor.csv`），少了看 `results/<tag>/qpdump_<host>.txt`。
  解析器只配对 RTS/RTR 态的 QP，残留的 ERR/RESET 态 QP 会让新 QP 绑到错的流集合。
- **交叉对流量必须走非 CM 路径**（`ib_write_bw -d <源设备> -x 3 … <目的IP>`，
  不能用 `-R`）。用 rdma_cm 时内核按**目的子网**先选路，选中的是拥有那个子网的
  VF，`-d` 指定什么都没用——流量实际是直连对，接收端归属到错的源 VF，而实验照常
  报完成。`cross_pair_net.sh` 的策略路由挡不住这条（它要匹配的源地址压根没被
  选中）。判据是接收端按 MAC 的归属：`-R` 给 vf1>vf1，`-x 3` 给 vf0>vf1。
- **流集合号只是名字**：注册表里有 flowtag 的用 flowtag，没有的（sgpu02 以外的主机的 vf4–vf7）
  用 (源, 目的) 字符串的 CRC32；代理与 `validation/distill.py` 用同一条规则。没有集合号的流集合既收不到
  预算也绑不上 QP，整场按未知额度跑。
- **PCC device 码改完要在每一台 DPU 上重编**
  （`meson setup --reconfigure build && ninja -C build pcc/doca_pcc`）。`ninja`
  单独不重编设备码，dpacc 是 configure 步。`deploy_check.sh --deploy` 会替源码
  变化的节点做，手工改则四台都要做。
- **重新 apply TCP 执行面之前先 `rm -rf /sys/fs/bpf/hpft_tcp_edt`**：pin 还在时
  `bpftool prog load` 失败，而 `tcp-shaper-apply` 只把失败记进结果、tc 仍指着
  旧程序——表现是"部署完了却没生效"。
- **四台机器的流量生成器必须同版本**。iperf 3.9 的客户端连不上 3.20 的服务端，
  报 `unable to send control message` 且一个字节都不发——那条流集合根本不存在，
  与"没要求跑"无法区分。`deploy_check.sh` 会比对四台的版本并检查 perftest fork。
- **一次只跑一场实验**。两场重叠时，后一场的角色重启会打断前一场，而前一场
  仍会写出一整套看起来完整的产物。`validation/run/run.sh` 用 `/tmp/hpft_run.lock`
  互斥。
- **不要在实验运行期间改它的脚本**。bash 边读边执行，改文件会让运行中的实例
  读到写了一半的内容。
- TCP 执行面在主机内核（"host fq+edt"），不在 DPU：OVS 占据 representor ingress，DPU 上没有既透明
  又保持 pacing 语义的挂载点。
- 接收端 DPU 上的分类 OpenFlow 规则、遥测通道等运行时状态重启会丢，
  `rx_agent` 与 `reboot_recover.sh` 会自动重装，不要手工删。

## 代码地图

**两端 agent 与分配器**
- `tools/dpu/rx_agent.py`、`tools/dpu/tx_agent_e.py` —— 收发两侧的控制面。
- `tools/dpu/fastfill.{c,py}` —— 三层分配（water-filling + 单遍天花板级联），
  两端共用。C 经 ctypes 加载，`.so` 缺失时代理拒绝启动（`deploy_check --deploy` 在各 DPU 上编它）；
  `fastfill.py` 里的纯 Python 版本只作 `fastfill_test.py` 的对拍参照。
- `tools/dpu/vport_meter.c` —— 1ms vport 计数器采样常驻进程。

**执行面**
- `tools/dpu/pcc/` —— DOCA PCC device 码（RDMA，跑在 DPA 上）。
- `tcp/bpf-opt3/` —— TCP 的 BPF EDT 实现。
- `tools/tcp_shaper/` —— TCP shaper 库与装载工具；`hpft_pace_shim.py` 与
  `deploy_check.sh` 从库里 import。
- `tools/host/hpft_pace_shim.py`、`qpn_resolver.py` —— host 侧的 BPF 写入桥与
  QP 归属解析，两者都是发送端必需。

**lab 编排**（都是 registry 驱动，加一台机器只需改 `nodes`）
- `tools/lab-infra/roles.sh` —— 角色编排：`all`（常设：每台都收发）/ `set --receiver <host> [--senders …]`（单接收端）/
  `status` / `stop`。按正确顺序拉起一个角色需要的全部东西。
- `tools/lab-infra/flowtag_probe.sh` + `registry_flowtags.py` —— 实测某台作为目的端时各直连对的 RDMA 流标签并写入注册表；每个新的接收主机都要做一次（标签与目的端有关）。
- `tools/lab-infra/deploy_check.sh` —— **跑实验前必过**：四台 DPU 与 host 侧
  载荷和 repo 一致、启用的 agent 健康且近期无 traceback、流量生成器同版本。
  代码不一致是致命的，配置不一致只是警告（runner 会推自己的场景配置）。
  `--deploy` 推送差异并在各自 Arm 上重编 `libfastfill.so` / `vport_meter` /
  `doca_pcc`。
- `tools/lab-infra/overlay.sh` —— 静态全网状 VxLAN overlay 的唯一实现，`--teardown`
  回到直连数据面。
- `tools/lab-infra/switch/` —— sn5600 的 QoS（`set_qos.sh`）与一分为二的切换脚本（V8 用，跑完切回）。
- `tools/lab-infra/vf_setup.sh` —— VF 重建（PF 地址、VF 功能、IP 全从 registry 读）。
- `tools/lab-infra/flow_preflight.sh` / `flow_postflight.py` —— 流对可用性守卫。
  RC 的错误完成是**终态**，被打死的 QP 不会自己回来，而每一层都还在报健康。
- `tools/reboot_recover.sh` —— DPU 重启 / fw reset 之后恢复易失状态：VF、TCP
  EDT、p1 的速率 / MTU / underlay 与遥测 IP / ARP 全互联 / PFC。**链路速率是
  断言而非假设**。
- `tools/cc_mode.sh` —— CC 模式切换（DCQCN ↔ PCC+HPFT，GBN ↔ SR）。
- `tools/lab_env.sh` —— 实验环境切换（`hpft` / `plain` 固件 DCQCN 基线 /
  `jakiro` DHTB 对照 / `ztr`、`swift` 本执行面的租户 CC 单独运行）。
- `tools/cross_pair_net.sh` —— 交叉对的策略路由（先读上面那条硬性规则）。

**验证与数据**
- `validation/` —— 控制设计的标准验证基准：八个场景、六条判据、runner、蒸馏器、画图、报告（`validation/README.md`）；
  `run/quick.sh` 是 12 秒一场的快跑器（`quick_sw.sh` 加交换机计数，`probe.sh` 场中读执行面），消融臂的定稿数据在 `results/perqp_executor_20260907/`；`tools/eval/` 是论文评估战役的编排库（`hpft-paper` 的 runner 用）。
- `config/lab-registry.json` —— 唯一真值：节点表、vnic、flowtag、政策、律参数。
- `config/lab-tcp-registry.json` —— TCP 执行面的 vnic 表（PF/VF PCI 地址在这里）。
- `results/<实验>_<UTC日期>/` —— 仍然有效的工程验证产物：`hw_maxrate_20260728`（devlink 单位）、
  `vf_caps_20260826`（VF 双端限速）、`perqp_executor_20260907`（执行面律的消融）；被后来的设计取代的实验只在 git 历史里。
  论文评估引用的实验在 `hpft-paper`。
