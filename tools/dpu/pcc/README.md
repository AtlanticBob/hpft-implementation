# DOCA PCC 执行面：租户拥塞控制的唯一实现 + HyperFront 的令牌桶

RDMA 的速率由 BlueField-3 DPA 上的 DOCA PCC 程序控制（`USER_PROGRAMMABLE_CC=1`）。`rp_rtt_template_dev_main.c` 一个文件里有两层：租户的拥塞控制逐 QP 运行（`0xccd 2` DCQCN、`0xccd 3` Swift、`0xccd 1` ZTR，都是每个 QP 一份状态机），以及 HyperFront 的令牌桶（设计 6 节，$r_i = c_i\min(1, R/\sum c_j)$，`0xcce 0 12` 关掉即"只跑租户 CC"）。

**这里的 DCQCN 与 Swift 是本仓库唯一算数的实现，任何"PCC 实现的 DCQCN/Swift"都指它们（2026-09-08 定稿）。** 不要再用 `~/bzx/pcc_swift_stock`、`~/bzx/pcc_ztr_stock` 这些原厂模板二进制当 CC 基线：原厂模板把 `algo_slot` 不为 0 的事件交给框架内置算法，而本 lab 里 96% 的数据 QP 事件带 slot 15，所以原厂二进制跑出来的是固件内置 CC 的成绩，不是它源码里那个算法。本文件处理所有槽位。

## 定稿数字（两台发送端各四个 RDMA 流集合打同一 200 G 端口，MTU 1024，overlay 上净荷上限 177.2 G）

| 拥塞控制 | 单独运行 | 加令牌桶 | 说明 |
|---|---|---|---|
| 固件 DCQCN（UPCC=0，真值） | 174.75 | 不适用 | 校准参照，`results/perqp_executor_20260907/` |
| DCQCN（`0xccd 2`） | 171.3，Jain 1.000（三场复跑 168.7 / 170.7 / 165.9，两场有个别流集合掉到 14 G） | 165.8，20.5 到 20.9 | 固件默认参数；丢包时目标速率一起重置（`0xcce 1 10`，默认），丢包型 meter 上 46.8 对固件 47.05 |
| Swift（`0xccd 3`） | 173.5，Jain 1.000（复跑 173.2） | 163.0，20.0 到 20.6 | 本 fabric 按论文 3.5/5.1 节调过：增量 256 B/RTT、流缩放范围 40 µs、窗口换算取 max(最新采样, 平滑往返)，目标 25 µs/β 0.8/最大减幅 0.5 为论文值 |

Swift 对 CNP 不做任何响应，只对 NACK 减窗。DCQCN 的参数与主机 PF `ecn/roce_rp` 的固件默认值逐项相同。

## 文件
- `rp_rtt_template_dev_main.c` — DPA 设备端（`doca_pcc_dev_user_algo`）。旋钮、读回、对照臂见 `docs/IMPLEMENTATION.md` 第 5 节。
- `pcc_host.c` — host 端，`HPFT_RATE_STDIN` 模式从 FIFO 读预算和旋钮并送邮箱，流集合预算行最新优先。
- `swift_stock/` — 原厂 `rp_rtt_template` 参考应用的 `algorithm_core` 套上 Swift（Kumar 等，SIGCOMM'20）后的源码：`rtt_template.c`（标记 `SWIFT_PORT`）、`swift_params.h`，`swift_port.py` 是生成它的补丁。执行面把这个核心编进来只作参考臂 `0xccd 4`（同样处理所有槽位；单独跑在八个流集合上是 153 G）。原厂模板二进制已从各 DPU 删除，不再是任何实验的臂。

**权威副本在 DPU**：`/home/ubuntu/bzx/doca34-apps/pcc/`，`tools/lab-infra/deploy_check.sh --deploy` 负责同步并重编（`meson setup --reconfigure build && ninja -C build pcc/doca_pcc`，`ninja` 单独不重编设备码）。
