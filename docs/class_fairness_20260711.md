# 类间公平性：从 0.25 到 1.02 的一夜（2026-07-11 深夜）

用户把话说得很清楚：RDMA 与 TCP 的类间公平是这个项目的第一目的，
其他指标都排在它后面。本文记录把 M2 场景的真实类间比例从严重失衡
（50ms 下 0.25、1ms 下 0.40）修到 1.02-1.05 的全过程：三个缺陷、
三个修复、验收数据、以及口径的选择。

## 口径：以 wire 为主，goodput 为辅

公平性在**线上字节**（含包头、含重传）口径上定义和验收。理由：VM
额度买的就是链路字节，调度器和标记的输入（vport 计数器）天然是
wire 口径；用 goodput 验收会把 EDT 乱序税（TCP 侧约 4% 虚假重传）
错记到公平性头上。goodput 同时报告——纯竞争实验证明两个口径给出
同样的结论（wire 1.02，app 1.00），且 app/wire 比值本身是数据通路
健康度的自检（RDMA 0.946、TCP 0.962，都只是包头开销）。

## 三个缺陷（按发现顺序）

**缺陷一：类归因失真（测量面，跨周期）。** megaflow 类构成比有
2 秒滞后（硬件计数器 1s 缓存 + 2s 滑窗），竞争期两类秒级交替占优时
它把每个瞬时总量都按时间平均（约 50/50）错切：TCP 突发 5G、RDMA
实际 1G 的时刻 rx 报 3/3。错误归因喂进水填充和标记，本身就是振荡的
助燃剂，还把 rx 侧的比例读数糊成漂亮的 1.06-1.15——历史 M2 的"通过"
就是在这个失真读数上打的分。修复：**接收端 host 跑一个速率导出
shim**（`tools/host/hpft_rate_exporter.py`，每 5ms 把各 VF 的 netdev
rx_bytes 快照连同读取时刻的时间戳经 tmfifo 推给 rx_agent）。判别
实验证明 RoCE 完全绕过内核 netdev 计数器（16.4GB 纯 RDMA 只动
2386 字节），所以 r_tcp = host 报告、r_rdma = vport 总量 − r_tcp，
类级拆分从此新鲜；megaflow mix 降级为类内多发送方细分与断流回退。
修完后竞争期立即出现教科书 1:1——但只维持到下一个缺陷发作。

**缺陷二：HAI 无视 grant 冲穿（响应律，跨周期，振荡的主引擎）。**
任何无标记 0.5 秒之后，HAI 倍增器以 16G/s 的墙钟斜率把 R 直冲 tree
上限（19.9G）——一次瞬时下探就够触发。冲穿 cap → VQ 饱和 → 标记
风暴 → MD 深砍 → 再来一轮，形成 15-25 秒周期的弛豫振荡。50ms 的
HAI 墙钟斜率一模一样，这就是两个周期的 M2 都振荡的原因。修复：
**AI/HAI 的探测上界锚在公平上限 ceil_f × probe_over_grant（1.05）**。
两个教训埋在这句话里：一，锚必须是 ceil 而不是需求封顶的 e_f——
e = min(fair, r×1.15) 通过发送端自己的 r 自指，执行面一点松弛就把
不动点拖向 0（实测：流被困在 0.27G 零标记，和 Q22 VQ 排空的死锁
同构），ceil 与自身 r 无关、恒 ≥ 公平份额，永不困人；二，余量必须
小（1.05）——试过 1.3，单流期 R 冲到 1.3×ceil，超额 1.7G 把 VQ
350ms 打满触发深砍，借用期变成 7.56↔2.28 的新振荡；1.05 的超额
（≤0.29G）VQ 只会温和滴标。ceil 已在 rx 端算好（Q22 就是为它引入
的），遥测记录从 (s, r, e) 扩为 (s, r, e, ceil) 带给发送端。

**缺陷三：转换期压穿 + RP 死锁（执行面，本日早些时候已修）。**
类进入/退出的转换期标记风暴把 R 打到 pace 地板，RP 被地板预算
wedge 后因"只在喂率变化时迈步"永不恢复。修复 = MD 下界 R≥0.3r +
RP 喂率交替抖动（见 control_period_1ms_status.md 追加二/三）。

## 验收数据（六项指标，75s 协议：RDMA 全程，TCP 在 [15,45]）

1ms、1:1 权重、三轮（逐轮新鲜 RP）：

| 指标 | rep1 | rep2 | rep3 | 标准 |
|---|---|---|---|---|
| 竞争段 wire 比例 | 1.04 | 1.03 | 1.05 | ±10% ✓ |
| 竞争段总利用率 | 5.86/5.82 | 5.88 | 5.84 | ~100% ✓ |
| 秒级振荡 stdev | 0.01/0.13 | 0.02/0.08 | 0.02/0.15 | 修前 1.9/1.6 |
| 借用（TCP 退出）| 1s 内回 5.98，min 5.97 | 同 | 同 | ≥90% ✓ |
| 回收（TCP 进入）| ~1.5s 让位到 2.99 | 同 | 同 | <20 周期 ✓ |
| rx-vs-客户端误差 | TCP 1%（2.88 vs wire 2.85）| — | — | <10% ✓ |

补充验收：**3:1 权重跟随**：rdma=4.48/tcp=1.39，比例 3.24（目标 3.0，
+8%，±10% 内）。**纯竞争 app 口径**：rdma 2.82 vs tcp 2.81（比例
1.00）。**单流回归**：M1b@1ms 5.66G 持平；M1b@50ms 5.11G 持平。
**50ms M2 同修复复验**：比例 0.25 → **0.95**（证明缺陷跨周期；1ms
仍全面更优：stdev 0.02 vs 0.09，借用 min 5.97 vs 4.86）。

## 新增运维件

`hpft-rate-exporter`（sgpu02 上的 systemd-run 瞬态 unit，看门狗已纳管）：
`sudo systemd-run --unit=hpft-rate-exporter /usr/bin/python3
/home/zhaoxiang/hpft/hpft_rate_exporter.py --registry
/home/zhaoxiang/hpft/lab-registry.json --local-host sgpu02`。
host 重启后需重启；registry 的 control 段新增 rate_export/
rate_export_port/rate_export_ms；rx_agent 收不到报告 >0.1s 自动回退
megaflow 归因（fail-safe）。注意 sgpu02 的 registry 副本在
`~/hpft/lab-registry.json`，下发 registry 时要多 scp 一份。

## 遗留

竞争期 RDMA 的 RC 重传与 TCP 的 EDT 乱序税仍是数据通路层的独立
工作线（app/wire 比值 0.946/0.962 是其量化基线）；M3（incast）与
多发送方场景下类内 mix 细分的新鲜度问题尚未复验。
