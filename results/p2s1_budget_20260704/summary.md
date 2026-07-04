# P2-1 进展:水位控制器(2026-07-04,进行中)

实现:per-pair O(1) 状态 {budget, level, r_ewma, epoch};所有流拿
rate=min(cc,level);epoch(1ms)从 ROCE_TX 事件累计聚合速率 R(per-thread
分片计数,EWMA α=1/4 平滑),R>B 比例收缩(限幅 1/2),欠载纯加性增长。
代码:pcc/device/hpft_rp_main_v2.c(权威副本)。

## 当前精度(budget=8G,goodput 期望 7.34)

| QP 数 | 聚合 | 误差 |
|---|---|---|
| 1 | 7.34 | 0% |
| 64 | 8.32 | +13% |
| 256 | 10.50 | +43% |
| 1024 | 11.26 | +53% |
| 64 @0.2G budget | 0.20 | ~0% |

## 排障记录(按时间序)

1. cap/N 均分废弃 → 水位控制器(不数 N,任意 QP 数 O(1))。
2. "hw ~17Mbps 地板"假说被单 QP 小速率测试证伪(2 units=0.38Mbps 精确执行);
   占空比机制随之废弃(且会饿死稀疏事件流)。
3. 16-bit sent_32bytes 截断假说 → pkts×132 估计(MTU 假设错,刷机后
   active_mtu=1024)→ EWMA 包大小学习(对/自适应)。
4. mailbox 调试响应通道(0xdeb)上线 → 直接观测 level/R/epoch。
5. **DPA 的 __atomic_exchange_n 编译通过但运行时语义坏**(epoch 永不认领,
   整个控制器死了却看不出来)→ 换竞态容忍认领。__atomic_compare_exchange_n
   直接编译报错。DPA 上仅 per-thread 分片 + 竞态容忍写法可靠。
6. 1ms epoch + 事件成波 → R 方差巨大 → 乘性增长复利爆炸(level 41↔328 振荡,
   时均 2.5×)→ EWMA 平滑 + 纯加性增长 + 收缩限幅,振荡消除。
7. 残余 N 相关超发(+13%~+53%):**CC 事件在高流数下被丢弃,TX 字节欠计**
   (64@0.2G 无损精确,负载越高丢越多)。
8. 端口计数器校正尝试失败:nic counters API 在 mailbox 上下文调用直接
   Fatal RPC(0xdec 崩掉 handler);事件上下文调用亦未验证成功。已回滚。

## 结论与下一步

水位机制本身已验证(小 N 精确、双 pair 独立、work-conserving 结构就位);
**残余误差是 R 测量输入问题,不是控制问题**。最优解与 P2-3 合流:
**接收端测 RX 速率(精确、无事件丢失)并随 cap 消息回传发送端**,替换
TX 事件估计作为控制器输入 —— 测量与接收端驱动 cap 用同一条 RTT 响应
负载通道。gating:sgpu02 DPU 的访问与 NP 部署。
备选:SN 序号缺口估计丢失率;事件上下文中的端口计数器(需查上下文约束)。
