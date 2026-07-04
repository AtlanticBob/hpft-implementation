# P2: DCQCN 重接(pair 级 CC)+ ctx 归属探测(2026-07-04)

## ctx 归属探测(format-5)

4 条不同 QP(0x6367-0x636a)映射到**同一 algo_ctxt 指针 0xd2090**,flowtag
同为 vf0 的 0x74249a41。确认 S3 结论:PCC 的 algo_ctxt 按 flowtag(=src
function)共享,非 per-QP。⇒ CC 状态必须做成 pair 级(放全局 pair 表),
不能用 algo_ctxt 存 per-QP CC——这与我们的 pair 粒度目标天然一致。

## DCQCN-lite 重接:rate = min(cc_rate, level)

- pair 表加 `cc_rate`(2^20 单位,初始 MAX);
- CNP 事件(DOCA_PCC_DEV_EVNT_ROCE_CNP)→ 乘性降(cc -= cc>>3,DCQCN 风格);
- 每 epoch(1ms)→ 加性恢复(cc += MAX>>8,即 256 epoch 回满);
- 输出 `results->rate = min(cc_rate, level)`:level 是 shaper 的 pair cap,
  cc_rate 是 fabric 拥塞响应,取小者——拥塞时降到 cap 以下,无拥塞时 = cap。

## 验证

| 阶段 | 吞吐 | 含义 |
|---|---|---|
| 无注入(cc=MAX) | 7.76G | rate=min(MAX,level)=level,**cap 零回归** |
| 冻结 cc=1G(0xccc) | 0.97G | rate=min(1G,level)=cc,**min() 公式生效** |
| 解冻 | 7.76G | cc 加性恢复回 MAX,**恢复机制正常** |

注入用 mailbox 0xccc <val> <idx>(设定并冻结 cc,模拟持续拥塞);单次注入
会被每毫秒恢复迅速拉回,故用冻结。真实 CNP 自动响应的端到端验证需要
ECN 标记 + incast 拥塞环境(当前点对点无拥塞不产生 CNP),CNP handler 逻辑
已就位并通过代码审查,标注为需拥塞环境的后续验证项。

## 遗留修复

CC 恢复原先误置于 level 控制块内(rx 稳定 do_ctrl=0 时不执行),已移到
每 epoch 独立执行。
