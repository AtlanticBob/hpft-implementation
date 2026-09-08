# Patch the stock rtt_template into a Swift (SIGCOMM'20) flow-level CC. Run on the DPU inside .../rtt_template/algo.
import re
p="rtt_template.c"; s=open(p).read()
if "SWIFT_PORT" in s: raise SystemExit("already patched")
hdr='''
/* ---- SWIFT_PORT (HyperFront lab, 2026-08-27) ----------------------------
 * Swift (Kumar et al., SIGCOMM'20) in place of the vendor rate rule. Each
 * PCC flow context (one per QP) is one Swift flow. Window in bytes, sent to
 * the NIC as rate = cwnd / rtt_s (fxp20, 2^20 = line rate = 25 B/ns).
 *   target = SW_BASE_TARGET + clamp(SW_ALPHA/sqrt(cwnd_pkts) - SW_BETA, 0, SW_FS_RANGE)
 *   rtt_s <  target                       : cwnd += SW_AI            (per RTT)
 *   rtt_s >= target, >= 1 rtt since last cut: cwnd *= 1 - min(SW_B * (rtt_s-target)/rtt_s, SW_MAX_MDF)
 *   NACK seen, same gate                  : cwnd *= 1 - SW_MAX_MDF
 * ECN/CNP is ignored (delay-only). Only the fabric-delay half of Swift
 * applies to RDMA. rtt_s is a 1/4 EWMA of the probe RTT (one probe per RTT;
 * the probe jitter on this fabric, sd 1-2 us, is the same order as the
 * base RTT, so raw samples would trigger a cut on every tail spike).
 * Thresholds are device-clock ns measured on THIS fabric (base RTT 2.7 us),
 * the executor compiles this core in as the 0xccd 4 reference arm. */
#include "swift_params.h"
static inline uint32_t sw_isqrt(uint32_t x)
{
	uint32_t r = 0, b = 1u << 30;
	while (b > x) b >>= 2;
	while (b) { if (x >= r + b) { x -= r + b; r = (r >> 1) + b; } else r >>= 1; b >>= 2; }
	return r;
}
/* context fields (cc_ctxt_rtt_template_t.reserved[]) */
#define SW_CWND(c)     ((c)->reserved[0])
#define SW_RTT_S(c)    ((c)->reserved[1])
#define SW_LAST_DEC(c) ((c)->reserved[2])
#define SW_N_DEC(c)    ((c)->reserved[3])
#define SW_N_NACK(c)   ((c)->reserved[4])
static inline uint32_t swift_core(cc_ctxt_rtt_template_t *ccctx, uint32_t rtt, uint32_t ts)
{
	uint32_t cwnd = SW_CWND(ccctx), rs = SW_RTT_S(ccctx), target, fs, s, rate;

	if (cwnd == 0) cwnd = SW_INIT_CWND;
	rs = rs ? rs + (uint32_t)(((int32_t)(rtt - rs)) >> 2) : rtt;
	SW_RTT_S(ccctx) = rs;
	s = sw_isqrt(cwnd >> 2);                       /* 16*sqrt(cwnd/1024) */
	if (s == 0) fs = SW_FS_RANGE;
	else { uint32_t a = (SW_ALPHA << 4) / s; fs = (a > SW_BETA) ? (a - SW_BETA) : 0; if (fs > SW_FS_RANGE) fs = SW_FS_RANGE; }
	target = SW_BASE_TARGET + fs;
	{
		int can_dec = (uint32_t)(ts - SW_LAST_DEC(ccctx)) >= rs;
		if (ccctx->flags.was_nack && can_dec) {
			cwnd = (uint32_t)(((uint64_t)cwnd * ((1u << 16) - SW_MAX_MDF)) >> 16);
			ccctx->flags.was_nack = 0; SW_LAST_DEC(ccctx) = ts; SW_N_NACK(ccctx)++;
		} else if (rs < target) {
			cwnd += SW_AI; ccctx->flags.was_nack = 0;
		} else if (can_dec) {
			uint32_t diff = rs - target, mdf;
			if (diff > (1u << 21)) diff = 1u << 21;
			mdf = (diff << 10) / ((rs >> 6) ? (rs >> 6) : 1u);      /* diff/rs in fxp16 */
			mdf = (uint32_t)(((uint64_t)mdf * SW_B) >> 16);
			if (mdf > SW_MAX_MDF) mdf = SW_MAX_MDF;
			cwnd = (uint32_t)(((uint64_t)cwnd * ((1u << 16) - mdf)) >> 16);
			SW_LAST_DEC(ccctx) = ts; SW_N_DEC(ccctx)++;
		}
	}
	ccctx->flags.was_cnp = 0;                      /* delay-only */
	if (cwnd < SW_MIN_CWND) cwnd = SW_MIN_CWND;
	if (cwnd > SW_MAX_CWND) cwnd = SW_MAX_CWND;
	SW_CWND(ccctx) = cwnd;
	/* rate (fxp20 of line) = cwnd / (rs * 25 B/ns) */
	rate = (uint32_t)((((uint64_t)cwnd) << 20) / ((uint64_t)(rs ? rs : 1u) * SW_LINE_BPNS));
	if (rate > DOCA_PCC_DEV_MAX_RATE) rate = DOCA_PCC_DEV_MAX_RATE;
	if (rate < SW_MIN_RATE) rate = SW_MIN_RATE;
	return rate;
}
/* ---- end SWIFT_PORT ---- */
'''
# insert before the entry point helpers (after algorithm_core definition ends: before "static inline void rtt_template_handle_roce_tx")
k=s.index("static inline void rtt_template_handle_roce_tx"); s=s[:k]+hdr+"\n"+s[k:]
# RTT handler: use swift_core with the event timestamp
old="	cur_rate = algorithm_core(ccctx, rtt, cur_rate, param, is_high_tx_util, norm_np_rx_rate);"
assert old in s
s=s.replace(old,"	(void)param; (void)is_high_tx_util; (void)norm_np_rx_rate;\n	cur_rate = swift_core(ccctx, (uint32_t)rtt, end_rtt);   /* SWIFT_PORT */")
# new flow: init the window too
old2="	ccctx->cur_rate = param[RTT_TEMPLATE_NEW_FLOW_RATE];\n"
assert old2 in s
s=s.replace(old2, old2+"	SW_CWND(ccctx) = SW_INIT_CWND; SW_RTT_S(ccctx) = 0; SW_LAST_DEC(ccctx) = 0;   /* SWIFT_PORT */\n")
open(p,"w").write(s)
open("swift_params.h","w").write('''#ifndef SWIFT_PARAMS_H_
#define SWIFT_PARAMS_H_
/* Swift parameters, device-clock ns / bytes / fxp16. Calibrated on the
 * HyperFront lab fabric (BF3 <-> SN5600 <-> BF3, VxLAN overlay, 200G):
 * probe base RTT 2744 ns, single-flow full-load band 3.7-3.9 us. */
#define SW_PKT          (1024u)               /* RoCE path MTU, bytes */
#define SW_LINE_BPNS    (25u)                 /* 200 Gb/s in bytes per ns */
#define SW_BASE_TARGET  (12000u)              /* ns */
#define SW_FS_RANGE     (20000u)              /* ns, flow-scaling span */
#define SW_ALPHA        (50000u)              /* ns: fs_range/(1/sqrt(4)-1/sqrt(100)) */
#define SW_BETA         (5000u)               /* ns: alpha/sqrt(100) */
#define SW_AI           (SW_PKT)              /* bytes per RTT */
#define SW_B            (26214u)              /* 0.4 fxp16 */
#define SW_MAX_MDF      (16384u)              /* 0.25 fxp16 */
#define SW_INIT_CWND    (64u << 10)
#define SW_MIN_CWND     (SW_PKT)
#define SW_MAX_CWND     ((1u << 20) - SW_PKT) /* line x 40 us */
#define SW_MIN_RATE     (1u << (20 - 14))
#endif
''')
print("patched")
