# HPFT Shaper v2 — Handoff (2026-07-09)

Single entry point for a new agent/session taking over. Read this first, then the
"Reading order" below. The project builds **transparent sender-side rate limiting**
for RDMA + TCP on a BlueField-3 DPU, granularity `{src_vnic_id, dst_vnic_id}`,
tenant-transparent, pacing (not drop-policing).

> **当前活跃工作（2026-07-09 起）**：EuroSys 投稿——方案 E（边缘虚拟队列政策
> 标记 + 统一响应律），在既有 shaper 之上做租户间/类间两层公平。设计定稿
> `docs/rd_fairness_design_e.md`；**实现交接入口 `HANDOFF_IMPL_E.md`**（含
> 已验证 lab 操作、已知 bug、里程碑）。下方原有内容（RDMA/TCP shaper 等）是
> 其依赖的既有系统，全部仍然有效。

## Status at a glance

| Track | State | One-liner |
|---|---|---|
| **RDMA shaper (PCC on DPA)** — main result | ✅ near-complete | Transparent, low-latency (idle/sat 2.77/2.79µs, zero latency-bw coupling), fair (spread 0), cap ≤3%, down-step ~5-7ms, ~100Hz+ control. Running in lab. |
| **TCP shaper (host fq+edt)** | ✅ done | fq + TC-BPF EDT on the host. 6.86G, sat latency 65-78µs, down-step 22.8ms, multi-flow fair. **Works but NOT tenant-transparent** (host kernel). Call it "host fq+edt" (not "opt3"). |
| **Unified controller** | ✅ done | One `{src,dst,rate}` JSON protocol; tcp→direct-bpf, rdma→PCC caps. |
| **T3.2 (TCP transparent DPU offload)** | ⏸️ PAUSED | Feasibility study complete with a **definitive negative result**. **Do NOT resume unless the user explicitly asks.** |
| **DPU NTP** | ✅ solved (2026-07-08) | host→DPU time push, all 4 clocks within ~15ms. |

## Hard rules (read before touching the lab)
- **Do NOT resume T3.2 / TCP DPU offloading** unless the user re-raises it. See
  `docs/t32_dpu_tcp_design.md` for why (deep-match needs `fdb_def_rule_en=0` = big rewrite).
- ~~Never touch `devlink port function rate`~~ **Re-verdict 2026-07-10** (user-authorized
  retest): works cleanly on fw 32.49.1014 (vf3, set/unset, precise 10G, no wedge). The
  2026-07-05 wedge did not reproduce; soak-test before production reliance. Now serves
  as layer-1 (per-VM sold-rate hard cap) of design_e §3.6 v2.1.
- Say **"host fq+edt"** or "host tc" for the TCP shaper — not "opt3".
- Experiment-env perturbations (reflash/restart services/long runs on the 2 hosts +
  2 DPUs) are pre-authorized — no need to re-ask each time.

## Lab facts
- **Hosts:** `sgpu01` (this repo, sender side) and `sgpu02` (BatchMode ssh works, receiver side).
- **DPUs:** `ssh hpft-dpu` = sgpu01's BF3 (192.168.102.2, sender/experiment DPU);
  `ssh hpft-dpu2` = sgpu02's BF3 (via sgpu02 jump, receiver). DPU `ubuntu` has
  **passwordless sudo**; host sudo is limited.
- **VFs:** host sgpu01 `dpu1vf0..3` = `10.1.{0..3}.1/24`; sgpu02 = `10.1.{0..3}.2`.
  vf0 RDMA = `mlx5_6` on host. On the DPU, vf0 rep = `pf1vf0`, uplink = `p1`
  (`pci/03:00.1`), all on OVS bridge `underlay-p1`.
- **Versions:** DOCA 3.4.0112, fw 32.49.1014. `ib_write_bw`: use
  `~/hyperfront/perftest-26015/ib_write_bw` (system 6.23 core-dumps).
- **PCC build (DPU):** `~/bzx/doca34-apps`; device-code change ⇒
  `rm -rf build && meson setup build -Denable_all_applications=false -Denable_pcc=true && ninja -C build`,
  then `bash /tmp/rp_service.sh start`. RP binary = `doca_pcc`. Introspection:
  `echo "0xdeb <idx>" > /tmp/rp_fifo` → RP log `HPFT_RSP`.
- **Persistent lab services:** RP (`doca_pcc`) + `tx_agent2` on hpft-dpu, `rx_agent`
  on hpft-dpu2; DPU time-sync cron on both hosts (`tools/dpu-timesync.sh`).

## Current lab state (verified 2026-07-08)
RP + tx_agent running, RDMA cap 6G on vf0, host fq+edt EDT on `dpu1vf0`, OVS kernel
(both bridges intact), 4 VFs up, host→net TCP ~6.2-6.9G. Lab clean/healthy.

## Reading order for a new agent
1. **This file** (HANDOFF.md).
2. **Auto-memory** (loaded each session): `hpft-shaper-v2-status.md` (the dense lab
   log + gotchas), `tcp-shaper-naming-and-scope.md` (naming + T3.2-paused rule).
3. **RDMA shaper** (main result): `docs/phase1_report.md`, `docs/phase2_report.md`,
   `docs/v2_report.md`; design `docs/p2_receiver_driven_design.md`.
4. **RDMA vs TCP comparison** (the story): `results/rdma_eval_20260705/summary.md`,
   `results/rdma_ratechange_20260705/summary.md`.
5. **TCP shaper (host fq+edt):** `docs/tcp_shaper_v2.md`, `results/tcp_opt3_20260705/`.
6. **Unified controller:** `docs/c1_controller.md`, `tools/hpft-unified-controller`.
7. **T3.2 (paused, for context only):** `docs/t32_dpu_tcp_design.md`, `tcp/dpu-punt/README.md`.

## Code / tools map
- `pcc/` — RDMA shaper: DPA device code (RP: `pcc/device/hpft_rp_main_v2.c`) + Arm host side.
- `tcp/bpf-opt3/` — host fq+edt TCP shaper (default). `tcp/dpu-punt/` — T3.2 paused reference (+ README).
- `tools/` — eval harnesses (`eval_rdma_shaper.sh`, `eval_tcp_shaper.sh`), controllers
  (`hpft-unified-controller`, `hpft-shaper-controller`), agents (`dpu/tx_agent2.py`,
  `dpu/rx_agent.py`), RP ops (`dpu/{rp,np}_service.sh`), `dpu-timesync.sh`, flash scripts.
- `results/<probe>_<UTCdate>/` — eval outputs + `summary.md`.
- `backup/` — OVS/DPU pre-change backups.

## Next-step options (from the last planning discussion; user leaning: consolidate)
1. **RDMA hardening** (clear engineering): DCQCN real reconnect (ECN/incast), 1024-QP
   under budget, dst-key P2-2, work-conservation/dual-pair formal re-test.
2. **Comprehensive eval + writeup**: multi-tenant/incast scenarios; RDMA-vs-TCP final
   comparison; T3.2 recorded as the definitive negative on coexistence-mode transparency.
3. **T4/T5** — previously deferred; pull their definitions before scheduling.
4. **T3.2 path C** (`fdb_def_rule_en=0` full FDB takeover) — ONLY if the user makes
   tenant-transparent TCP a hard requirement. Big, whole-node blast radius.
