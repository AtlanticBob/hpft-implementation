# CC 模式切换教程：非 PCC DCQCN ↔ PCC+HPFT、GBN ↔ SR（2026-07-15）

这份文档配合 `tools/cc_mode.sh` 使用，把 motivation 实验期间摸清的固件
行为固化下来，让后续实验不再踩坑。**从 2026-07-15 起，lab 的默认停留态
是"非 PCC 固件 DCQCN + GBN"**（motivation 实验基线）；PCC+HPFT 只在明确
需要时用 `cc_mode.sh pcc` 拉起，实验后不再自动恢复。

## 三个必须知道的固件事实

**1. `USER_PROGRAMMABLE_CC=1` 时"停掉 PCC 应用"不等于回到 DCQCN。**
UPCC=1 把 CC 引擎切到 DPA；doca_pcc 不在时固件跑的是它内部的 DPA 算法
——表现为贴瓶颈线速、零丢包、RoCE 帧几乎 100% 被标 CE 却没有 CNP 响应、
对一切 ECN/RP/NP 参数无感。要拿到真的固件 DCQCN，必须 UPCC=0 并做固件
reset。真 DCQCN 的指纹：单流 92.5G 时交换机标记率只有 ~0.06%，且接收端
PF 的 `np_cnp_sent` 与发送端 PF 的 `rp_cnp_handled` 逐个对账。

**2. mlxconfig 的改动，DPU Arm 上 `reboot` 不会生效。** Arm 软重启不
复位 NIC 固件（mlxconfig 的 Current 列纹丝不动）。正确做法是在 host 侧
走 `activate-fw.sh` 同款流程（cc_mode.sh 已内置）：

```bash
echo 0 > /sys/bus/pci/devices/0000:38:00.1/sriov_numvfs
mlxfwreset -d 38:00.0 --yes --sync 1 reset    # Arm 随之重启，~4 分钟
```

fw reset 之后必须重做：两侧 VF 重建 + MTU（动机实验用 1500）、dpu2 p1
降回 100G（瓶颈）、两端 DPU p0/p1 的全局 pause 关 + PFC 全零（lossy
前提；fw reset 会把 pause 复位成开）。cc_mode.sh 的 post_recover 全包。

**3. host VF 的 DCQCN 参数只认 host PF 的 ecn sysfs。** 要调 RP 恢复
参数（rpg_time_reset / rpg_ai_rate / rpg_hai_rate 等），写 sgpu01 上
`/sys/class/net/dpu1/ecn/roce_rp/*`（`dpu1` = 38:00.1 的 PF netdev），
运行时立即生效、fw reset 后回默认。DPU Arm 侧 p0/p1 的同名 sysfs、
debugfs cc_params 对 host VF 流量一律无效（2026-07-14 逐一探测排除）。
NP 侧（CNP 生成）同理在 sgpu02 的 `/sys/class/net/dpu1/ecn/roce_np/`。

## 常用操作

```bash
tools/cc_mode.sh status        # 双端 UPCC/SR 的 current+next、服务、链路
tools/cc_mode.sh gbn           # 切 GBN（ROCE_ACCL 寄存器，双端秒切，无 fw reset）
tools/cc_mode.sh sr            # 切 SR（同上；只对新建 QP 生效）
tools/cc_mode.sh dcqcn gbn     # 一键回"非 PCC DCQCN + GBN"基线（默认停留态）
tools/cc_mode.sh pcc           # 拉起 PCC+HPFT 全栈（UPCC=1 + E 栈三件套 + RP）
```

- `dcqcn` 幂等：Current 已经正确就不做 fw reset（顺手把漂移的
  next-boot 值也改齐）。`gbn`/`sr` 是寄存器写，天然幂等。
- 一次 fw reset 周期 ≈ 5-6 分钟（双端并行）。
- `pcc` 拉起后 tx agent 可能短暂 fail_open（空载 RTT 尖峰病，自愈）；
  soak/watchdog cron 两个方向都不自动碰，需要时手动去 crontab 注释/解注
  （`#MOTIV#` 前缀）。
- 交换机 ECN 档位与 CC 无关，单独用
  `paper/motiv_hetero_cc_20260714/motiv12_env.sh ecn off|neutral|aggr|gentle`
  切（off=ecn_debug，neutral=ecn_incast_bzx 108K/396K/20%）。

## 验证切换是否真的生效

- 旗标层面：`cc_mode.sh status` 看 Current 列（不是 Next Boot）。
- 行为层面（真 DCQCN vs DPA 算法）：单流 ib_write_bw 打 100G 瓶颈，
  交换机 ECN 标记率 ~0.06% 且 CNP 对账 = 真 DCQCN；标记率 ~100% 且改
  RP 参数无感 = 还在 DPA 算法。

## SR 的正确开启方法：ROCE_ACCL 寄存器（2026-07-15 终版）

**一条命令、立即生效、不动 CC，所有新建 QP 直接获得选择性重传：**

```bash
# 双端 host 各执行一次（新建 QP 生效，存量 QP 不变）：
sudo mlxreg -y -d 38:00.1 --reg_name ROCE_ACCL --set "selective_repeat_forced_en=0x1"   # SR
sudo mlxreg -y -d 38:00.1 --reg_name ROCE_ACCL --set "selective_repeat_forced_en=0x0"   # 切回 GBN
# 或直接用封装：tools/cc_mode.sh sr / gbn（自动双端）
```

三条实测定案（fw 32.49.1014）：

1. **真 SR**：单流 × RED 20K/60K/5% 丢弃：1.3-1.6 万次丢包事件下
   goodput 91G、线上字节 ≈ goodput（GBN 对照组线上多 ~5.6G 被接收端
   丢弃的乱序垃圾）——接收端接纳乱序、发送端只补洞。
2. **与 DCQCN 完全兼容**：forced SR 流照常被 host PF 的 DCQCN RP 参数
   控制（glacial 探针拍到 0.003G），CC 路径零变化。
3. **与 `RDMA_SELECTIVE_REPEAT_EN` mlxconfig 旗标完全无关**：flag=0
   下寄存器照样给 SR。lab 常态保持 flag=0（纯默认固件配置）即可。

**唯一注意**：寄存器是易失的——任何 `mlxfwreset`/掉电后归零（实测），
fw reset 后要重设。`cc_mode.sh status` 可随时核对。

16 流 RDMA incast 的干净对比（同 DCQCN、同普通建连、唯一变量=寄存器，
数据在 `paper/motiv_hetero_cc_20260714/rdmaonly3_*`）：默认 ECN 下
92.5G vs 91.2G（SR 略低 ~1.3%）；关 ECN 纯 tail-drop 下 **32.3G vs
91.2G（2.8 倍）**——GBN 臂 7.1 万次 NAK、RTT 4.8ms、线上满线速全是
重传；SR 臂零 NAK、RTT 0.77ms。附带观察：forced SR 的发送管线似乎
自带更克制的在途管理（noecn 下站立队列稳定在 ~9.6MB 不再打爆缓冲，
deep-RED 4-8MB 探针下队列甚至不过阈值），对比实验解读时留意。

## 附带的环境卫生事实

- **VF node GUID 为零会让 librdmacm 段错误**（rping/rdma_client 等
  一切 rdma_cm 应用崩在设备匹配）。已在两台 host 的 `vf_setup.sh`
  固化 GUID 分配（sgpu01=00:11:22:33:01:...、sgpu02=02:...）+ VF
  驱动 rebind，VF 重建自动生效。与 SR 无关，纯粹的环境修复。
