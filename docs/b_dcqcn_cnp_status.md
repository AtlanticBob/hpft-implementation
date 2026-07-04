# B:DCQCN 拥塞控制的 CNP 触发 —— 状态与未来验证(2026-07-04)

用户指示:CNP 的真实生成暂不尝试,但代码必须保留,未来某阶段验证。本文
记录已就位的机制、已验证的部分、以及未来触发真实 CNP 所需的环境条件。

## 已实现并保留的机制(pcc/device/hpft_rp_main_v2.c)

`rate = min(cc_rate, level)`,其中 level 是 shaper 的 pair cap,cc_rate 是
pair 级 DCQCN-lite 拥塞项:

- **CNP 事件降速**:`if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_CNP)` →
  `cc_rate -= cc_rate >> 3`(乘性降,DCQCN 风格),地板 HPFT_MIN_LEVEL。
- **加性恢复**:每 epoch(1ms)`cc_rate += DOCA_PCC_DEV_MAX_RATE >> 8`
  (256 epoch 回满线速),独立于 level 控制步执行。
- **输出**:`results->rate = min(cc_rate, level)`。

## 已验证(冻结注入,不依赖真实拥塞)

mailbox 0xccc `<val> <idx>` 设定并冻结 cc_rate,模拟持续拥塞:

| 阶段 | 吞吐 | 结论 |
|---|---|---|
| cc=MAX(无注入) | 7.76G | rate=min(MAX,level)=level,cap 零回归 |
| cc=1G 冻结 | 0.97G | rate=min(1G,level)=cc,**min() 公式正确** |
| 解冻 | 7.76G | cc 加性恢复,**恢复正确** |

即:min() 公式、CNP handler 的降速算术、恢复机制均已验证正确。**唯一未
端到端验证的是"真实拥塞自动产生 CNP → handler 被触发"这一环。**

## 未来触发真实 CNP 所需条件(下阶段)

1. **拥塞点**:当前点对点 200G 链路,单/双流不拥塞。需要 incast——多个
   src 同时打满争抢一个瓶颈(如多 VF → 单接收端口,或收窄链路)。
2. **ECN 标记**:交换机/接收端需对拥塞包打 ECN,接收端 NP 据此生成 CNP
   回送发送端。当前 PCC 接管了 CC,需确认:
   - PCC 模式下 CNP 的生成路径是否仍工作(可能需接收端 NP 侧显式生成,
     或固件 ROCE_CC 配置);
   - `PCC_INT_EN`、ECN marking 相关 mlxconfig 是否需调整。
3. **验证方法**:造 incast → RP 用 format-trace 计数 CNP 事件到达 →
   观察 cc_rate 自动下降 → 聚合被压到 cap 以下且拥塞缓解后恢复。

## 风险评估

- CNP 生成在 PCC-managed 模式下可能需要额外配置甚至不被支持(固件把 CC
  交给了 DPA);若结构不可得,诚实结论为"handler 就位,本平台无法自动触发,
  需外部 CNP 源"。这是 B 未来验证的主要不确定点。
- 冻结注入已证明:一旦 CNP 到达,系统响应正确。所以剩余风险纯在"CNP 能否
  到达",不在"到达后是否正确"。
