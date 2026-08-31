# validation 执行手册

结论、环境表、打流表和判据都在 `README.md`；这里只写怎么把 lab 摆到 README 描述的状态、怎么跑、怎么复原。

## 跑前准备（按顺序，每一批开始前做一遍）

1. `bash tools/lab-infra/deploy_check.sh --deploy`：四台 DPU 与 host 上的代码和仓库一致，agent 健康。
2. `bash tools/lab_env.sh status` 应显示 environment: HPFT（四台 UPCC=1、doca_pcc 在跑、overlay 在）；不是就 `bash tools/lab_env.sh hpft`。
3. `bash tools/cc_mode.sh sr`：四台 host 的 ROCE_ACCL selective_repeat_forced_en=1（易失，fw reset 后归零）。
4. `bash tools/lab-infra/vf_caps.sh sync`：32 个 VF 的 50 G 双端限速。
5. `bash tools/lab-infra/roles.sh set --receiver sgpu02 --senders sgpu01,sgpu03,sgpu04`。
6. 四台 host 每个 VF 的 TCP 执行面恰好一份当前程序：`tc filter show dev dpu1vfN egress` 只有一个 handle，tag 与仓库一致（`tools/host/edt_ensure.sh` 会自动纠正）。
7. 交换机：`swp37s0` 绑 `motiv_default_ecn`，无 egress-scheduler，四个 host 口绑 `motiv-nopfc`，pause 关（`nv show interface swp37s0 qos`）。
8. 注册表政策是标准政策（每 VM 权重 1、50 G、类 1:1），`e_params` 是这次要测的版本；runner 会把当次 registry 复制进 results/<tag>/。
9. 接收端 DPU 的 `hpft-vport-meter` 在跑、`vpm_sample.py` 在 hpft-dpu2:/tmp/。
10. `bash tools/lab-infra/dpu_time_sync.sh apply && sleep 40 && bash tools/lab-infra/dpu_time_sync.sh status`：四台 DPU 的钟对齐到各自 host（偏差应在 ±2 ms 内）。DPU 镜像没有任何时间同步，钟差会让接收端和发送端日志的对比多出几十到几百毫秒的假滞后。
11. V4 之前确认 perftest-26015 的 `--rate_limit 10` 在 1 个 QP 上确实压到 10 G；V6 之前确认四台 DPU 执行面能切 `0xccd 3`、三台发送端 BBR 模块可用。

## 跑法

```
bash validation/run/run.sh V2_flowset_join_leave V2_conf5_20260829_r1
python3 validation/distill.py V2_conf5_20260829_r1
python3 validation/plot/timeline.py V2_conf5_20260829_r1
```

每跑完一个场景停下汇报，再跑下一个。

## 复原

V6 之后把执行面切回 `0xccd 0`、iperf3 不再带 `-C bbr`。其余场景不改任何常设配置，不需要复原。
