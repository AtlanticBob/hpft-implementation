# M0 — 逐流集合计数验收（方案 E，design_e §5.3）

日期：2026-07-09（UTC）。新 `tools/dpu/rx_agent.py`（M0 骨架）部署于
hpft-dpu2 `/opt/hpft/`，systemd-run 临时单元 `hpft-m0-rx`，T=50ms（20Hz）。

## 验收结果：通过

**标准**：各流集合速率误差 <3%；读取耗时 <T/3（16.7ms）。

| 对照 | meter（线上字节） | 期望值 | 误差 |
|---|---|---|---|
| A：TCP 单流 vf0（iperf3 20s，6.83G） | 18658.7 MB | 18659.1 MB | **−0.00%** |
| B：RDMA 单流 vf0（ib_write_bw 20s，5.53G @6G cap） | 14619.2 MB | 14611.4 MB | **+0.05%** |
| C：混合 — vf0 TCP（7.19G） | 23871.0 MB | 23870.8 MB | **+0.00%** |
| C：混合 — vf1 RDMA（188G，见缺陷 a） | 588164.5 MB | 587495.8 MB | **+0.11%** |
| A/B/C 全部 vs vport 硬件计数器交叉验证 | — | — | **−0.00%**（全部） |

- 期望值折算：TCP = iperf3 接收 goodput × 1514/1448 + 重传段 × 1514
  （重传字节在线上真实存在，goodput 不含；run A 重传 285779 段恰好解释
  432MB 差额）。RDMA = BW avg × D × 1.0569（active_mtu=1024：每 1024B 载荷
  58B 头 + 每消息 16B RETH）。**perftest `-D` 模式的 iterations 列不可靠**
  （≈真实消息数一半），必须用 BW avg × D。
- C 中 vf0 RDMA（被缺陷 b 压穿的流）meter=503MB vs 自报 146MB：非测量误差
  （vport 交叉 −0.00%）——被压到 47Mbps 地板的 QP 触发 RC 重传风暴，线上
  字节 ≈3.4× goodput。r_f 语义本就应计线上到达（含重传），此为特性非缺陷。

**读取耗时**：p50=0.9ms p95=1.1 p99=1.2 max=89.8（n=3055）。达标关键：
经 ovs-vswitchd unixctl socket 常驻连接直发 JSON-RPC `dpctl/dump-flows
type=offloaded`（0.9ms），替代每 tick fork `ovs-appctl`（~30ms）。

## 顺带产出：tx_agent2"长跑卡死"bug 根因（M1b 交付的前置取证）

Run C 中活体复现，两个独立缺陷，均已实锤：

- **缺陷 (a)——vf1 cap 从未生效**：dpu2 旧 rx_agent DEVS 表 vf1 键为陈年
  IP `10.1.0.4`（应为 `10.1.1.2`），caps 查不到 → 上报 cap=0 → tx_agent2
  对 cap=0 静默跳过 → vf1 从不进 mailbox → vf1 RDMA 完全脱管（实测单流
  185.13G，应为 4G）。strace 实锤：tx 每 tick 只写 3 entry
  （`0xb47c0003` = batch 头"3 个 entry"——交接记录"只更新 pair 3"即此误读）。
  **临时修复已做**：sed 改 `/tmp/rx_agent.py` + 重启 hpft-rxagent，复测
  vf1 = 3.65G（<4G cap ✓）。
- **缺陷 (b)——同 VF 的 TCP 把 RDMA 压穿到地板**：tx_agent2 用发送端
  representor `rx_bytes` 测 pair 线速率，该计数是 **TCP+RDMA 之和**，而
  执行器只有 PCC（只压 RDMA）。同 VF TCP 超过 RDMA cap 时（host fq+edt
  ~6.9G > 6G），RP 积分控制持续压 level 直到 min 地板（bud/128≈47Mbps，
  ib_write_bw 显示 0.044G）。复现时间线（本 meter 逐 tick 数据，1s bin）：
  RDMA 稳 6.0G → TCP 注入 ~2s 内压穿至 ~0 → TCP 停止 ~2s 恢复 6.0G。
  0xdeb 内窥见 lvl 26345→31457 爬回全额。
- 两缺陷都是**常驻性**的（restart 后 1h 内即可复现），与"运行 4 天"无关：
  "restart 即愈"与 (b) 的"竞争 TCP 停止后 ~2s 自恢复"时间上一致。不能
  100% 排除另有慢性退化模式；新 tx_agent（M1b）加日志/看门狗兜底。
- 结构性根治属方案 E 本体：per-class 流集合把 TCP/RDMA 的测量与执行分开
  （M1b/M2），registry 驱动配置消灭手维护的 DEVS 表，s_f=0 显式许可让
  "缺 pair"变得可见。**缺陷 (b) 是论文动机的现成案例**（类盲测量面 +
  单类执行器的必然故障）。

## 诊断性发现（记录，不处理）

- host fq+edt 路径 TCP 在 6.8-8G 下有 ~1.3-1.6% 段重传（iperf3 retrans
  字段，wire 字节吻合）。
- 混合场景下 perftest `-D` 的实际发送时长可长于/短于 D，自报 BW 已按其内部
  时长归一。

## 文件

- `analyze.py` — 验收计算（含折算公式）；`hpft_rxagent_m0.jsonl` — 逐 tick
  流集合速率（20Hz，含 repro 时间线原始数据）
- `runA_*` / `runB_*` / `runC_*` — 三组验证运行的自报输出与 vport 快照
- `reproB_*` — 缺陷 (b) 复现（35s RDMA，10-20s 注入 TCP）

## 复现命令

- agent：`sudo systemd-run --unit hpft-m0-rx python3 /opt/hpft/rx_agent.py`
- 规则自装验证：`sudo ovs-ofctl --strict del-flows underlay-p1 "priority=40,ip"`
  后启 agent → `rules_added=[(40,'ip')]`
- 流量：iperf3 -c 10.1.0.2；`~/hyperfront/perftest-26015/ib_write_bw
  -d mlx5_6/7 -p 1851x --report_gbits -D <s> 10.1.{0,1}.2`（server 先起）

## 结束时 lab 状态

hpft-m0-rx 观测单元继续运行（只读，0.9ms/50ms tick，JSONL 在 dpu2
/tmp/hpft_rxagent_m0.jsonl）；旧 hpft-rxagent 已打 vf1 补丁重启（vf1 4G cap
恢复生效）；三条分类规则在位；hpft-txagent / RP / host fq+edt 未动
（缺陷 (b) 在旧链路中仍潜伏，属 M1b 根治范围）。
