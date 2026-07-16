# 类级带宽测量重设计:改动与现状报告

2026-07-13 · 面向在本 lab 上同步跑测试的同学 · 数据与脚本都在本目录

## 一句话

接收端 DPU 的 TCP/RDMA 类级速率测量,从"vport 总量 − 主机报来的 TCP(跨机相减)"
切换为"vport 硬件计数器 ib/eth 分桶直读(DPU 本地)",**已接管生产控制环**;
接收主机上的 `hpft-rate-exporter` 已退役(disabled,可一键回滚)。

## 对同步测试的直接影响(先读这段)

1. **hpft-dpu2 上多了一个常驻 systemd 单元 `hpft-vport-meter`**。它每 1ms 把
   4 个 VF 的分桶字节计数写进 `/dev/shm/hpft_vpm`,新版 rx_agent 默认读它。
   请不要 stop 它当"清理"——它挂了不炸系统(agent 自动降级),但类瞬态精度
   会退回旧水平。
2. **sgpu02 的 `hpft-rate-exporter` 是故意停的**。soak watchdog 日志里
   `exporter=inactive` 是预期状态,不是故障,不要顺手拉起来;拉起来也无害
   (它退居归因链第二级),但会让对照实验解释变复杂。
3. **rx_agent 日志(`/tmp/hpft_rxagent_e.jsonl`)新增两个字段**:
   - `ma`:本拍由 vport 直读归因的 dst VF 数(有流量时应等于活跃 VF 数);
   - `rv`:每个 dst VF 的 `[rdma_bps, kern_bps]` 原始直读速率(未经类内细分),
     做归因对账直接用它。
   `ha` 从此正常情况**恒 0**;如果看到 ha>0,说明有人把导出器重新启用了。
4. **测量健康速查**:有流量而 `ma=0` → `systemctl status hpft-vport-meter`。
   此时控制不中断(megaflow 回退,类拆分带 ~2s 滞后),修好 helper 约 1s 内
   自动恢复直读。
5. **所有现有 runner 不需要改**:`systemd-run … /opt/hpft/rx_agent.py` 的
   invocation 原样可用;`--vport-meter ''` 可显式关闭新路径。
6. **不要起第二个 vport_meter 写同一个 mmap 文件**(双写会撕 seqlock)。
   本目录里带 `start_recorders` 的 `run_incast8.sh / run_flip.sh /
   run_multisrc.sh / run_failsafe.sh` 是切换前的旁路对照脚本,它们会自己起
   一个 helper——现在只适用于"回滚旧 rx_agent 后做对照"的场景,日常别跑。
   切换后的闭环验证请参考 `run_cl.sh`。

## 改了什么

| 位置 | 改动 |
|---|---|
| `tools/dpu/vport_meter.c`(新增) | ~200 行 C:对 esw manager(mlx5_1)裸发 DEVX `QUERY_VPORT_COUNTER`(0x770,内核 uid=0 白名单,不改驱动/数据面),~110µs/vport;每 1ms 查 vport 1..4,把(写方单调钟时间戳,rx_ib/rx_eth/tx_ib/tx_eth 字节)按 per-vport seqlock 记录发布到 mmap 文件 |
| `tools/dpu/rx_agent.py` | 新增 `VportMeter` 读者类;类归因优先级 **vport 直读 > 主机导出器 > megaflow 构成比**,各级按 0.1s 新鲜度自动降级;`--vport-meter` 默认 `/dev/shm/hpft_vpm`;日志加 `ma`/`rv` |
| `docs/design_and_implementation.md` §6.1 | 改写为三代演进(megaflow-only → 相减 → 分桶直读),含数字 |
| 部署(hpft-dpu2) | `/opt/hpft/{rx_agent.py, vport_meter, vport_meter.c}`;systemd 单元 `hpft-vport-meter` enabled;旧版备份 `/opt/hpft/rx_agent.py.pre_vpm.bak` |
| 部署(sgpu02) | `hpft-rate-exporter` disabled;unit 文件保留于 `/etc/systemd/system/`(注释里有回滚命令) |
| 不变 | 发送端 tx_agent、RP/mailbox、遥测格式、registry、所有 runner |

## 为什么换

旧法里 RDMA 是个**余数**:TCP 在主机测、总量在 DPU 测,两个点、两个时钟、两种
窗宽相减,残差全部记到 RDMA 头上。实测在"TCP 在流、RDMA 静默"的场景,旧法凭空
报出中位 0.7G、p95 近 2G 的幻影 RDMA(约为 TCP 速率的 3%),给 water-filling
喂了一份不存在的类需求;外加每台接收主机一个常驻导出器 + tmfifo 通道的运维
负担。新法两个类都是直接硬件计数(RoCE 记 ib 桶、内核路径记 eth 桶),同点同钟,
没有相减,也没有跨机部件。

## 验证摘要(全部对主机侧独立真值,原始数据在本目录)

- **语义与准确**:vport ib/eth 桶与主机 `port_rcv_data`/`netdev rx_bytes` 的
  累计字节**逐字节一致**(比率 1.00000);类纯度零泄漏。
- **新鲜度**:固件活计数,0.25ms 采样每读必新(旧认知里的 1s 缓存只属于
  megaflow 流表计数,0.87ms 台阶只属于 rep sysfs 缓存路径)。
- **incast8**(8 流 100G root,90s)稳态 1s 窗:RDMA mae 0.24%、偏置 +0.09%
  (旧法 0.44%、+0.26%);直读覆盖率恒 4/4。
- **同 VF 类交替**(旧法死角):RDMA 静默平台期幻影 p95 **0.000G**(旧法
  1.945G);平台期 mae RDMA 0.17% vs 旧 1.87%。
- **闭环验收**(新测量控制 vs 切换前基线,均以真值评判):利用率 87.7→87.9G、
  TCP 振荡 std 2.94→2.84G、TCP 塌陷秒数相同——**不劣化**。
- **故障演练**:kill helper → 0.1s 内降级 megaflow 无缝接住,重启 ~1s 恢复,
  全程零异常、r_f 无断档。

## 两个悬案的裁决

- **"incast 下 RDMA 系统性比 TCP 低 ~1G"**:是**真实份额差**(真值本身
  TCP ~12.0G vs RDMA ~10.6G/流),不是相减偏置。测量侧结案,问题移交控制/CC 域。
- 竞争期归因:旧法在 exporter 健康时其实基本准(incast 稳态 ≈ 真值),它的
  结构性失效在"一类静默"场景(幻影)与 exporter 陈旧回退时;新法两处都干净。

## 移交控制域的遗留观察(非测量问题,本次实验中拿到的副产品)

1. incast8 中 TCP 每 90s 有 ~5s 真实塌零片段,新旧归因下完全相同(系统固有
   动态,不是测量伪影)。
2. 同 dst 的两条 RDMA 流在 RP 层严重不公平:第二条加入把在位流从 44G 压到
   ~2G,离开 5s 后仅恢复到 12.5G。
3. 已知局限(设计上接受):**类内**多发送方细分仍靠 megaflow 构成比,源变动
   后 ~3s 收敛。

## 回滚手册(每步独立可做,归因链自动降级保证安全)

```
# 1) 恢复旧 rx_agent(hpft-dpu2)
sudo cp /opt/hpft/rx_agent.py.pre_vpm.bak /opt/hpft/rx_agent.py
#    然后按惯例重启 hpft-rxagent-e(systemd-run 瞬态单元)
# 2) 重新启用主机导出器(sgpu02)
sudo systemctl enable --now hpft-rate-exporter
# 3) (可选)停掉直读 helper(hpft-dpu2)
sudo systemctl disable --now hpft-vport-meter
```

## 事故记录

步骤④过程中误重跑了一次旁路版 `run_flip.sh`,覆盖了步骤③a 的原始数据文件
(其分析结论已留存于当日汇报),并造成 ~30s 的 mmap 双写窗口;闭环 flip 已在
确认单写者后干净重跑,验收数据不受影响。这也是上文"不要起第二个写者"提醒的
由来。

## 仓库状态

以下改动**未提交**:`tools/dpu/rx_agent.py`、`tools/dpu/vport_meter.c`、
`docs/design_and_implementation.md`、`results/measure_v2_20260713/`(本目录)。
工作树里另有此前 MIMD 战役遗留的 `tools/dpu/tx_agent_e.py` 改动,与本次无关,
提交时需分开处理。
