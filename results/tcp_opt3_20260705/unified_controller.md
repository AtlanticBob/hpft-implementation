# T2:统一常驻 controller(2026-07-05)

`tools/hpft-unified-controller` — 一个常驻进程、一套 `{src_vnic,dst_vnic,rate_bps}`
JSON 协议,协议无关地分发到两个后端:

- **TCP** → in-process `DirectBpfMapWriter` 直写 pinned opt3 EDT map(`/sys/fs/bpf/hpft_tcp_edt`),
  稳态 **34–47µs/次,永不 fork**(满足降速性能报告的硬性要求)。
- **RDMA** → PCC receiver-cap 路径:改写 receiver DPU 上 rx_agent 热读的 caps 文件
  (DOCA PCC on BF3 DPA)。ssh 改 caps ~25ms(RDMA 控制路径固有)。

控制协议(stdin JSON-lines,每行一条):
```
{"cmd":"set","proto":"tcp","src_vnic":..,"dst_vnic":..,"rate_bps":..} -> {"ok":true,"latency_us":..}
{"cmd":"set","proto":"rdma",...}                                       -> {"ok":true,"latency_us":..}
{"cmd":"get"} / {"cmd":"quit"}
```

启动:`--apply` 从两份 registry 下发初始速率;`--control` 起常驻回路;二者可组合。
启动时预热 TCP map fd,首次 set 不冷开。

活体验证(单流经 vf0,通过常驻回路改速):

| 命令 | 控制延迟 | 数据面 |
|---|---|---|
| set tcp 10G | 314µs(冷)→预热后 <50µs | 9.06G ✓ |
| set tcp 6G | 95µs | 5.69G ✓ |
| set tcp 3G | 71µs | 2.97G ✓ |
| set tcp 1G | 65µs | 0.57G ✓ |
| set rdma | 25ms(ssh caps) | 分发可达 ✓ |

留待后续:两份 per-proto registry 合并为单一 schema(vnic 同时带 rdma
representor/flowtag 与 tcp pf_bdf/netdev);当前按 proto 原生 id 寻址,功能完整。
