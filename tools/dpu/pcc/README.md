# DOCA PCC 执行面：租户拥塞控制的唯一实现 + HyperFront 的令牌池

RDMA 的速率由 BlueField-3 DPA 上的 DOCA PCC 程序控制（`USER_PROGRAMMABLE_CC=1`）。`rp_rtt_template_dev_main.c` 一个文件里有两层：租户的拥塞控制逐 QP 运行（`0xccd 2` DCQCN、`0xccd 3` Swift、`0xccd 1` ZTR，都是每个 QP 一份状态机），以及 HyperFront 的令牌池（设计 6 节，$r_i=\min(c_i,(R+P)/N)$，`0xcce 1 12` 关掉即"只跑租户 CC"）。旋钮、回读与对照臂的清单在 `docs/IMPLEMENTATION.md` 第 5 节。

**这里的 DCQCN 与 Swift 是本仓库唯一算数的实现，任何"PCC 实现的 DCQCN/Swift"都指它们。** 原厂 `rp_rtt_template` 模板二进制不作任何实验的臂：模板把 `algo_slot` 不为 0 的事件交给框架内置算法，而本 lab 里 96% 的数据 QP 事件带 slot 15，原厂二进制跑出来的是固件内置 CC 的成绩，不是它源码里那个算法。本文件处理所有槽位。

## 租户拥塞控制单独运行的数字（两台发送端各四个 RDMA 流集合打同一 200 G 端口，MTU 1024，overlay 上净荷上限 177.2 G）

| 拥塞控制 | 单独运行 | 说明 |
|---|---|---|
| 固件 DCQCN（UPCC=0，真值） | 174.75 | 校准参照，`results/perqp_executor_20260907/` |
| DCQCN（`0xccd 2`） | 171.3，Jain 1.000（三场复跑 168.7 / 170.7 / 165.9，两场有个别流集合掉到 14 G） | 固件默认参数；丢包时目标速率一起重置（`0xcce 1 10`，默认），丢包型 meter 上 46.8 对固件 47.05 |
| Swift（`0xccd 3`） | 173.5，Jain 1.000（复跑 173.2） | 本 fabric 按论文 3.5/5.1 节调过：增量 256 B/RTT、流缩放范围 40 µs、窗口换算取 max(最新采样, 平滑往返)，目标 25 µs/β 0.8/最大减幅 0.5 为论文值 |

加上账本与令牌池之后的数字在 `validation/STATUS.md`（V1 两种拥塞控制各自的贴合、净荷与标记）。

Swift 对 CNP 不做任何响应，只对 NACK 减窗。DCQCN 的参数与主机 PF `ecn/roce_rp` 的固件默认值逐项相同。

## 文件
- `rp_rtt_template_dev_main.c` — DPA 设备端（`doca_pcc_dev_user_algo`）：逐 QP 的租户拥塞控制、按 (vhca_id, QPN) 键的 QP 记录表、每流集合的令牌池、邮箱旋钮与回读。
- `pcc_host.c` — host 端，`HPFT_RATE_STDIN` 模式从 FIFO 读预算和旋钮并送邮箱，流集合预算行（`0xb47f`）最新优先，回读以 `HPFT_RSP` 行打印到 `/tmp/pcc_rp.log`。

**权威副本在 DPU**：`/home/ubuntu/bzx/doca34-apps/pcc/`，`tools/lab-infra/deploy_check.sh --deploy` 负责同步并重编（`meson setup --reconfigure build && ninja -C build pcc/doca_pcc`，`ninja` 单独不重编设备码），重编后要 `bash /opt/hpft/rp_service.sh start` 才跑新码。
