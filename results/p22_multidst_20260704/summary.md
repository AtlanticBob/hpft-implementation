# P2-2 多 dst 维度验证(2026-07-04)

## 关键发现:flowtag 编码 dst

同一 src VF(vf0)、同源 IP(10.1.0.1),到两个 dst vNIC 的 flowtag **不同**:
- vf0→10.1.0.2: flowtag 0x74249a41
- vf0→10.1.0.4: flowtag 0x3100bbe8

说明 PCC flowtag 是流级 hash(含 dst),非纯 per-src-function。S3 时每 VF 单 dst,
flowtag 看似 per-src 实为 per-{src,dst}。两条 P2-2 路径由此都成立;本次用
qpn override 完整验证(通用,不依赖 flowtag 语义)。

## 机制(qpn override,已实现)

1. **host qpn_resolver**(sgpu01):`rdma res show qp` 本地 {lqpn→rqpn} join
   对端 {lqpn(=rqpn)→dst_ip},得 {src_lqpn → dst_ip}。纯管理面,不碰租户。
2. **mdst_controller**(sender DPU):(src,dst)→pair_idx;下发 0xB48D 显式 pair
   {idx, flowtag=0, dst_tag, cap, rx_rate} + 0xB48E qpn 映射 {qpn→idx}。
3. **RP 双级查找**:事件 qpn 查哈希表命中→pair_idx;未命中→flowtag 扫描(回落,
   单 dst 零回归)。R 用接收端 per-dst representor 速率(发送端 vport 无法分 dst)。

## 验证(vf0 单源 IP → 两 dst 同子网,独立 cap)

| dst | cap | 稳态 wire | 误差 |
|---|---|---|---|
| dstA (10.1.0.2) | 6G | 5.99G | -0.2% |
| dstB (10.1.0.4) | 2G | 2.00G | 0% |

RP pair 表:pair0 bud=31457/R=31513、pair1 bud=10486/R=10432,两 pair 独立收敛。
单 dst 回归(重构后 flowtag 路径):vf0 7.99G / vf1 4.00G,无损。

## 约束与工程记录

- 发送端 representor vport 是 per-src-VF,多 dst 时 R 必须用接收端 per-dst。
- 多 src 到同一 dst 时接收端 per-dst 是聚合(需 per-(src,dst) 才能分)——当前
  拓扑每 dst 单 src,干净;更一般情形留作后续。
- resolver 必须在有 SSH 能力的登录环境跑(systemd 裸环境无 HOME/.ssh → peer=0);
  C1 整合时并入统一 controller 并解决托管。
- 验证拓扑:sgpu02 vf1 加第二 IP 10.1.0.4/24 使两 dst 同子网,vf0 单源 IP。
