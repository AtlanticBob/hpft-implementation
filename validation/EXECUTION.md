# validation 执行手册

结论、环境表、打流表和判据都在 `README.md`；这里只写怎么把 lab 摆到 README 描述的状态、怎么跑、怎么复原。

## 跑前准备（按顺序，每一批开始前做一遍）

1. `bash tools/lab-infra/deploy_check.sh --deploy`：四台 DPU 与 host 上的代码和仓库一致，agent 健康。
2. `bash tools/lab_env.sh status` 应显示 environment: HPFT（四台 UPCC=1、doca_pcc 在跑、overlay 在）；不是就 `bash tools/lab_env.sh hpft`。
3. `bash tools/cc_mode.sh sr`：四台 host 的 ROCE_ACCL `selective_repeat_forced_en=1`。**所有 HPFT 实验一律跑 SR**，这一步不是可选项。寄存器易失，fw reset / Arm 重启后归零。`run.sh` 会逐台复核并在不满足时中止，所以漏做只会让运行失败，不会静默跑成 GBN。查看用 `mlxreg --reg_name ROCE_ACCL --get`，**不要看 `lab_env.sh status` 的 `SR current=`**（那是 mlxconfig 的另一个开关，本 lab 永远为 0）。
4. `bash tools/lab-infra/vf_caps.sh sync`：32 个 VF 的 50 G 双端限速。
5. `bash tools/lab-infra/roles.sh set --receiver sgpu02 --senders sgpu01,sgpu03,sgpu04`。
6. 四台 host 每个 VF 的 TCP 执行面恰好一份当前程序：`tc filter show dev dpu1vfN egress` 只有一个 handle，tag 与仓库一致（`tools/host/edt_ensure.sh` 会自动纠正）。
7. 交换机：`swp37s0` 绑 `motiv_default_ecn`，无 egress-scheduler，四个 host 口绑 `motiv-nopfc`，pause 关（`nv show interface swp37s0 qos`）。
8. 注册表政策是标准政策（每 VM 权重 1、50 G、类 1:1），`e_params` 是这次要测的版本；runner 会把当次 registry 复制进 results/<tag>/。
9. 接收端 DPU 的 `hpft-vport-meter` 在跑、`vpm_sample.py` 在 hpft-dpu2:/tmp/。
10. `bash tools/lab-infra/dpu_time_sync.sh apply && sleep 40 && bash tools/lab-infra/dpu_time_sync.sh status`：四台 DPU 的钟对齐到各自 host（偏差应在 ±2 ms 内）。DPU 镜像没有任何时间同步，钟差会让接收端和发送端日志的对比多出几十到几百毫秒的假滞后。
11. 四台 host 的 iperf3 必须带 `--start-at` 补丁（源码 `~/hyperfront/iperf320`）。**这个选项不在 `--help` 里**，补丁只加了选项与解析、没加帮助文本，所以要这样查：`strings /usr/local/lib/libiperf.so.0 | grep start-at`。`run.sh` 开跑前会自己验一遍，缺了直接中止。
12. V8 之前确认接收端 sgpu02 的 vf0/vf1 两个 meter 是 50 G（`vf_caps.sh status`），并且 `sgpu01` 的 vf0、vf1 这两对在场景里跑得熟——**不要拿没接好的 VF 做这个实验**：在 vf5 上试过，接收端算出了入场额 25 G，而发送端 agent 的 Ehat = 0、许可速率停在 2 G 地板、RDMA 执行面里根本没有这个 pair 的条目，四条 QP 走"未知流"兜底额度跑到 19.36 G，是许可速率的 9 倍，许可速率完全没生效。
13. V4 之前确认 perftest-enhanced 的 `--rate_limit 10` 在 1 个 QP 上确实压到 10 G（RoCE 上硬件限速一定被拒，要在 stderr 上看到 "providing SW rate limit" 那行才算数）；V6 之前确认四台 DPU 执行面能切 `0xccd 3`、三台发送端 BBR 模块可用。

## 跑法

```
bash validation/run/run.sh V2_flowset_join_leave V2_conf_20260901_rep1
python3 validation/distill.py V2_conf_20260901_rep1
python3 validation/plot/timeline.py V2_conf_20260901_rep1
```

V8 有两个配置，图名要分开（`timeline.py`/`trust.py` 的第二个参数就是图名前缀）：

```
bash validation/run/run.sh V8_hidden_bottleneck V8a_<tag>            # 配置 A：置信度开
HPFT_RDMA_TRUST_STEP=0 \
  bash validation/run/run.sh V8_hidden_bottleneck V8b_<tag>          # 配置 B：置信度冻结在零
bash validation/run/run.sh V8_hidden_bottleneck_tcp V8t_<tag>        # TCP 版：vf0 跑 TCP
python3 validation/plot/timeline.py V8a_<tag> V8a
python3 validation/plot/trust.py    V8b_<tag> V8b
```

V9 同样两个配置，对照的开关换成到期步长（README §四 V9）：

```
bash validation/run/run.sh V9_trust_exit V9a_<tag>                   # 配置 A：过期项开（默认）
HPFT_RDMA_TRUST_DECAY=0 \
  bash validation/run/run.sh V9_trust_exit V9b_<tag>                 # 配置 B：过期项关（旧规则对照）
python3 validation/plot/timeline.py V9a_<tag> V9a
python3 validation/plot/trust.py    V9a_<tag> V9a
```

runner 自己在窗口起止时改接收端的 meter、跑完恢复 50 G，EXIT 陷阱保证异常中止也会恢复；对照配置结束时把 `g_trust_step` 写回 66、`g_trust_decay` 写回 13。跑完核对 `results/<tag>/hidden_meter.txt` 与 `trust_arm.txt` 是不是这次要的值。

每跑完一个场景停下汇报，再跑下一个。

## 复原

V6 之后把执行面切回 `0xccd 0`、iperf3 不再带 `-C bbr`。V8/V9 的三样东西 runner 自己收尾（接收端 meter 回 50 G、`g_trust_step` 回 66、`g_trust_decay` 回 13），但跑完还是核一眼 `bash tools/lab-infra/vf_caps.sh status`，四台的八个 meter 都该是 50000000 kbps。其余场景不改任何常设配置，不需要复原。
