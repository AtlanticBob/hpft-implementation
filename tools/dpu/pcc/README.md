# HPFT DOCA PCC 反应点算法(RDMA 的 DCQCN + budget 兜底)

RDMA 的速率由 BlueField-3 DPA 上的 DOCA PCC 程序控制(USER_PROGRAMMABLE_CC=1,
替代内置 DCQCN)。HPFT 在 NVIDIA 的 `pcc_rp_rtt_template_app` 参考算法上改写,
实现 `results->rate = min(cc_rate, level)`:level = tx_agent 下发的 budget/QP,
cc_rate = DCQCN 式对 ROCE_CNP 的乘减兜底。

## 本目录文件(DPU 源码的 tracked 副本)
- `rp_rtt_template_dev_main.c` — DPA 设备端主逻辑(`doca_pcc_dev_user_algo`)。
- `pcc_host.c` — host 端(含 `HPFT_RATE_STDIN` 模式:budget 从 stdin 读)。

**权威副本在 DPU**:`hpft-dpu:/home/ubuntu/bzx/doca34-apps/pcc/`。改这里后要同步。

## 关键修法:cc_rate 降速按 QP 数归一化(2026-07-13)
原实现的 bug:`cc_rate` 是 **per-pair**(每 flowtag 一个),却收 **per-QP** 的
CNP → N 个 QP 的 CNP 全砸一个 cc_rate → 聚合降幅随流数放大 → 高流数下公平崩。

修法(`doca_pcc_dev_user_algo` 的 CNP 分支 + `hpft_pair_t.qp_count`):
```c
uint32_t nqp = c->qp_count ? c->qp_count : 1;
uint32_t nr = c->cc_rate - ((c->cc_rate >> 6) / nqp);   /* 温和基准 + 归一化 */
```
`qp_count` 在 qpn_map 新 QP 插入时 ++。两要素缺一不可:`>>6` 温和基准匹配
TCP 丢包退让、`/nqp` 让聚合降幅与流数无关。实测对称 incast 16-256 流 = 0.98-0.99,
GBN/SR 无差异。见 auto-memory `dcqcn-rp-fix`。

## 构建 + 加载(在 DPU 上)
```
cd /home/ubuntu/bzx/doca34-apps
meson setup --reconfigure build      # 设备码由 configure-time run_command 经 dpacc 编
ninja -C build pcc/doca_pcc
# rp_service.sh start 会加载 build/pcc/doca_pcc
```
注意:`ninja` 单独不会重编设备码(dpacc 是 configure 步),必须先 `meson --reconfigure`。
