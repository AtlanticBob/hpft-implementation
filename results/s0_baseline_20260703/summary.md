# S0 RDMA 基线(2026-07-03)

无 PCC 运行,固件默认 CC。sgpu01 → sgpu02,RDMA WRITE,65536B 消息。
物理口 p0/p1 均为 200 Gb/s;196 Gb/s ≈ 200GbE 线速 goodput。

| case | 配置 | 结果 |
|---|---|---|
| bw_vf0_q1 | vf0↔vf0, 1 QP, 10 s | 196.03 Gb/s |
| bw_vf0_q64 | vf0↔vf0, 64 QP, 10 s | 196.03 Gb/s |
| bw_vf1_q1 | vf1↔vf1, 1 QP, 10 s | 196.03 Gb/s |
| bw_vf0_p4 | vf0↔vf0, 4 进程 × 1 QP 并发 | 4 × 49.01 = 196.04 Gb/s(完美均分) |
| lat_vf0 | vf0↔vf0, 2B write, 10k 次 | typical 2.40 µs / avg 2.60 / p99 3.57 / p99.9 6.21 µs |

命令与原始输出见本目录 `*.log`;runner 为 `tools/run_s0_baseline.sh`。
注意:perftest duration 模式(`-D`)必须两端一致,否则参数协商失败。
