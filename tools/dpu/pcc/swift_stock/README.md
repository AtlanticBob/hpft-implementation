# Swift on the stock DOCA PCC RTT template (motivation 1-2 "Swift" arm)

A copy of the vendor `rp_rtt_template` reference application with Swift
(Kumar et al., SIGCOMM'20) in place of the vendor rate rule. No HyperFront
code: same tree, same host application, same probe machinery as the ZTR arm
(`~/bzx/pcc_ztr_stock`); only `algo/rtt_template.c` differs, plus the
parameter header. One PCC flow context (= one QP) is one Swift flow, so a
10-QP pair is ten independent Swift flows - the paper's per-connection
semantics. (The executor-side port in `rp_rtt_template_dev_main.c`, `0xccd 3`,
treats a pair as one flow; that is why it sat at 88% port utilisation while
this binary reaches 98%.)

- `rtt_template.c` - the patched file (marker `SWIFT_PORT`)
- `swift_params.h` - the fabric-calibrated constants
- `swift_port.py` - the patch applied to the pristine file (kept for provenance)

Build on a DPU: `cp -r ~/bzx/pcc_ztr_stock ~/bzx/pcc_swift_stock`, drop the
two files into `pcc/device/rp/rtt_template/algo/`, mark the now-unused
`algorithm_core` `__attribute__((unused))`, then
`meson setup build -Denable_all_applications=false -Denable_pcc=true && ninja -C build pcc/doca_pcc`.
The binary is architecture-only (aarch64 + DPA blob), so one build serves all
DPUs: `~/bzx/pcc_swift_stock/build/pcc/doca_pcc`. `lab_env.sh swift` starts it
on every sender DPU (the receiver runs the HPFT executor as PCC responder).

Calibration and validation: `results/swift_20260827/summary.md`.
