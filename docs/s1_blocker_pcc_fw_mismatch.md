# S1 阻塞:doca_pcc 启动失败 —— 固件/DOCA 版本错配(2026-07-03)

## 现象

`doca_pcc`(RP 模式)在 PCC 上下文创建阶段失败,所有组合下 syndrome 一致:

```text
flexio_create_prm_outbox: Failed to create outbox PRM object. Status is 0x3, syndrome 0x6d0fd5.
doca_pcc_start: Failed to create Flex IO process, please check USER_PROGRAMMABLE_CC
                in mlxconfig, or existing PCC application!, doca_err = DOCA_ERROR_DRIVER
```

## 排查证据链(全部实测)

| 检查 | 结果 | 排除的假设 |
|---|---|---|
| `USER_PROGRAMMABLE_CC`(pciconf0 与 0.1,`-e q`) | Default=False, **Current=True, Next Boot=True** | 未开启/未生效 |
| `PCC_INT_EN` | False(0)(RP 不受影响,NP 也满足要求) | 固件内部 PCC 抢占 |
| `DPA_AUTHENTICATION` | False(0) | DPA 签名拦截 |
| 现存 PCC 进程 / DPA 进程(pgrep、dpa-ps) | 无 | PCC 槽位被占 |
| `mlxprivhost q` | 无限制 | 权限限制 |
| **flexio_rpc 最小样例(自建,mlx5_0)** | **成功**(DPA 上执行 7+35=42) | 通用 FlexIO/DPA 故障 |
| doca_pcc @ DPU Arm mlx5_0(pcc supported) | 同样 outbox 失败 | 设备选择错误 |
| doca_pcc @ DPU Arm mlx5_1 | 同样失败(该函数 caps 无 pcc 段) | — |
| **host 侧自建 doca_pcc(DOCA 2.9.2, x86)@ bf1 卡 mlx5_5(b8:00.1,NIC mode,caps: pcc supported)** | **同样 outbox 失败,syndrome 相同** | 单卡固件状态 / DPU-mode 特有问题 |

版本事实:

- 三张卡固件一致:**32.47.2682**(2026 年,DOCA 3.2/3.3 配套代;BSP 4.9.3 原配固件应为 32.43.x)。
- DPU Arm:DOCA **2.9.3008** + flexio-sdk 24.10.2454;host:DOCA **2.9.2005**。
- 即:固件比用户态新了约 4 个 DOCA 版本。通用 FlexIO ABI 兼容(rpc 样例可跑),
  但 PCC 专用的 FlexIO process/outbox 创建接口不兼容 → 固件拒绝(syndrome 0x6d0fd5)。

## 决定性补充证据(2026-07-03 下午)

DPU 上发现前人工作 `/home/ubuntu/bzx/pcc_fixed_rate_10g`(2026-05-28,含
EXPERIMENT_LOG.md):当时在**同一台 DPU、同一个 DOCA 2.9.3008** 上,固定速率
PCC 实测把 host PF 流量从 177.13 Gbps 压到 9.18 Gbps,SIGINT 退出后恢复——
PCC 曾经完全可用。今天重跑**当时验证过的同一个二进制**,报同样的
outbox/0x6d0fd5 失败。结论:5/28 之后有人把固件升到 32.47.2682,破坏了
DOCA 2.9 的 PCC。该日志同时确认了 S2 探测的可行性先例(fixed-rate 控速有效、
`algo_slot=0xf` 的 fallback 陷阱、以及 in-tree build.sh 的构建配方,均可复用)。

## 结论

fw 32.47.2682 与 DOCA 2.9 的 PCC 库不兼容。要在本 lab 跑 PCC,必须让固件与
DOCA 用户态回到同代。候选路径:

1. **刷新 DPU BFB 到 DOCA 3.3 bundle**(官方路径,最干净):固件+Arm 全套用户态
   一致;代价是 DPU 系统重装(OVS 配置、sudoers、hpft 工具需重建),需 host 侧
   rshim 操作和 maintenance window。
2. **容器方案(低扰动)**:host 拉 DOCA 3.3 arm64 容器镜像 → 经 tmfifo 传入 DPU
   (DPU 无外网,镜像几 GB,tmfifo 慢)→ 特权容器内运行 doca_pcc。不动 DPU 系统;
   残余风险:3.3 用户态 vs 4.9.3 BSP 内核驱动的兼容性(PCC 主要走 devx,预计可行)。
3. **host 开 NAT 给 DPU + apt 升级 DPU 的 DOCA 到 3.3**:半升级,混版本风险
   (OVS-DOCA 等连带),NVIDIA 不推荐跨大版本 apt 升级 BSP 组件。
4. **固件降级回 32.43.x**:不推荐——32.47 是有人在 BSP 之外特意升的,降级可能
   影响其他使用者,且需电源循环。

## 环境备注

- DPU 无外网:默认路由走 tmfifo(192.168.102.1),oob_net0 无地址,10.0.4.1 不通。
- host(sgpu01)有外网;host 侧完整 sudo 需密码(仅 v1 tcp-shaper 工具在免密白名单)。
- host 侧 doca_caps:dpu 卡(38:00)与 bf0 卡(16:00)host PF 均 pcc unsupported
  (DPU mode,CC 归 Arm);bf1 卡(b8:00)host PF pcc supported(NIC mode)——
  这也是为什么 bf1+host 是干净的对照实验点。
- host 侧构建工作区:`pcc/build-host/apps`(git 忽略,产物 `build/pcc/doca_pcc`)。
