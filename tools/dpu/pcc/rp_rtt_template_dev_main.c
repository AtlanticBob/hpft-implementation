/*
 * Copyright (c) 2023-2026 NVIDIA CORPORATION AND AFFILIATES.  All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification, are permitted
 * provided that the following conditions are met:
 *     * Redistributions of source code must retain the above copyright notice, this list of
 *       conditions and the following disclaimer.
 *     * Redistributions in binary form must reproduce the above copyright notice, this list of
 *       conditions and the following disclaimer in the documentation and/or other materials
 *       provided with the distribution.
 *     * Neither the name of the NVIDIA CORPORATION nor the names of its contributors may be used
 *       to endorse or promote products derived from this software without specific prior written
 *       permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR
 * IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
 * FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL NVIDIA CORPORATION BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 * BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 * OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
 * STRICT LIABILITY, OR TOR (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 */

#include <doca_pcc_dev.h>
#include <doca_pcc_dev_event.h>
#include <doca_pcc_dev_algo_access.h>
#include "pcc_common_dev.h"
#include "rtt_template.h"

#define DOCA_PCC_DEV_EVNT_ROCE_ACK_MASK (1 << DOCA_PCC_DEV_EVNT_ROCE_ACK)

/* ======================================================================
 * HyperFront RDMA executor (design v4 section 6)
 *
 * The card is configured with ROCE_CC_SHAPER_COALESCE_P2=SOURCE_QP on the port
 * that carries tenant traffic, so ONE PCC FLOW IS ONE QP: the tenant
 * congestion control (DCQCN, Swift or ZTR, selected by mailbox 0xccd) runs
 * per QP with exact signal attribution, and HyperFront keeps its own
 * granularity, the FLOW SET.
 *
 * The law: one TOKEN POOL per flow set, refilled at the rate R the
 * receiver's ledger set. Each QP pays the pool for the stretch it has just
 * covered at the rate it was pacing, and is then capped at
 *
 *      r_i = min(c_i, (R + pool) / N)
 *
 * with c_i its own CC's rate, the pool level folded into a rate over one
 * millisecond and bounded to a quarter of a millisecond of R, and N the
 * QPs of the set that sent in the last millisecond. Nothing here ever
 * raises a QP above its own c_i: HyperFront only caps the set at R, and
 * the tenant CC keeps the whole job of reacting to the network below that
 * cap. The equal-cap arm (r_i = min(c_i, R/N), no pool) and the equal-split
 * arm (r_i = R/N, CC ignored) are the ablation arms of the design (mailbox
 * 0xcce <n> 22); 0xcce 1 12 runs the tenant CC alone.
 *
 * QP records are keyed by (vhca_id, qpn) and placed by a hash of that key
 * (the framework's per-flow context is only a cache hint: it is not
 * strictly one per QP, and the DPA has no atomics to arbitrate a shared
 * allocator with).
 * ====================================================================== */

#define HPFT_MIN_RATE    (2u)        /* fxp20 floor the wire can still carry */
/* Bytes a managed port carries in one microsecond at line rate: the token
 * pool is the only place the device needs an absolute unit. Platform
 * quantity, like HPFT_MIN_RATE - 200 Gb/s on this fabric. */
#define HPFT_LINE_B_PER_US (25000u)
/* a QP silent longer than this contributes nothing to its own rate account */
#define HPFT_PACC_MAX_US   (100000u)
#define HPFT_EPOCH_US    (1000u)     /* per-QP and per-set housekeeping tick */
#define HPFT_QSLOTS      (1024)      /* per-QP records */
/* Keyed records are placed in the first HPFT_QHOME of them. A sender holds
 * tens of QPs against 128 slots, so the table stays sparse and the whole
 * live set can be read back one slot at a time; the rest of g_q is what the
 * round-robin fallback hands out, and 0xdf3 reports how far it has gone. */
#define HPFT_QHOME       (128)
#define HPFT_SETS        (32)        /* flow sets this sender serves */
#define HPFT_SET_QPS     (128)       /* QPs listed per flow set */
#define HPFT_CTX_MAGIC   (0x48505131u)  /* "HP11" in the QP's own context */
#define HPFT_QP_STALE_US (2000000u)  /* a QP silent this long leaves its set */
#define HPFT_QP_GONE_US  (8000000u)  /* and this long: its record may be reused */
#define HPFT_SET_EMPTY   (0xffffffffu) /* a retired entry in a set's member list */

/* which tenant CC the executor runs for every QP (mailbox 0xccd) */
#define HPFT_CC_ZTR   (1u)
#define HPFT_CC_DCQCN (2u)
#define HPFT_CC_SWIFT (3u)
static volatile uint32_t g_algo = HPFT_CC_DCQCN;

/* 0xcce <0|1> 12: run the tenant CC alone, HyperFront ignored. This is the
 * control arm and also how the CC models are validated on their own. */
static volatile uint32_t g_cc_only;
/* 0xcce <units> 21: what a QP whose flow set is not known yet may send.
 * The agent computes it as R0 / expected QPs per flow set (design v4 5.4,
 * R0 = h*C) and re-asserts it every 5 s; this compiled-in value is the
 * same number for h = 8 %, four QPs (2^20 / 50 = 4 G on a 200 G port) and
 * only matters between an executor restart and the agent's first push. */
static volatile uint32_t g_unknown_rate = (DOCA_PCC_DEV_MAX_RATE / 50u);
/* 0xcce <n> 22: the law. 0 = the token pool (default); 1 = equal cap,
 * r_i = min(c_i, R/N), no sharing inside the set; 2 = equal split ignoring
 * the CC, r_i = R/N. 1 and 2 are the ablation arms. */
#define HPFT_LAW_TOKEN  (0u)
#define HPFT_LAW_CAP    (1u)
#define HPFT_LAW_EQUAL  (2u)
/* 3 = the proportional pool (2026-09-10, under test as an arm): the same pool,
 * the same refill and the same payment, but the set's allowance is divided by
 * the SUM of its members' CC rates instead of by their count, and each QP gets
 * its own share of that sum:
 *   r_i = min(c_i, c_i * (R + pool) / sum_cc)
 * sum_cc is the sum the per-set epoch already keeps (sum c_j over the QPs
 * drawing), so no new arithmetic crosses QPs. That sum is up to a millisecond
 * stale, and the pool is what makes that harmless: if the sum is off by a
 * factor k the set sends R/k, the pool moves, and it settles exactly where
 * (R + pool)/sum_cc equals R over the true sum - the k cancels and every QP
 * ends at c_i * R / sum c_j, which is the design's formula. The pool depth
 * bounds how far a stale sum can be corrected (a quarter of a period of R,
 * so +-25 % of R); beyond that the correction saturates. */
#define HPFT_LAW_PROP   (3u)
/* The flow set owns a pool of bytes refilled at R; a QP may send at its own
 * CC's rate as long as the pool can pay for it. Nothing here needs sum c_j:
 * the volatile quantity - each QP's rate, which a delay-based CC changes
 * every round trip - never enters an arithmetic that has to be consistent
 * across the set. What the grant does need is how MANY QPs are drawing, and
 * that is a count the epoch already keeps and that does not move every round
 * trip.
 *   ceiling = (R + whatever the pool has saved up) / nlive
 *   r_i     = min(c_i, ceiling)
 * A QP that wants less than its equal share simply leaves bytes in the
 * pool, and every QP's ceiling rises by its share of them - the
 * redistribution happens inside the same millisecond instead of one epoch
 * later. The pool is refilled at R and charged by what was ALLOWED: each
 * QP pays the pool for the interval it has just covered at the rate it was
 * pacing, so the pool holds the integral of R minus what the set was
 * allowed to send, and a ceiling
 * derived from it is a plain integral controller with no gain to choose.
 * The depth, a quarter of a millisecond of R, bounds the burst a set may
 * take after an idle stretch. */
static volatile uint32_t g_law = HPFT_LAW_TOKEN;
/* a QP that sent within this long is drawing tokens (0xcce <us> 24);
 * ablation knob, 1 ms is the design point */
static volatile uint32_t g_active_us = 1000u;
#define HPFT_ACTIVE_US  (g_active_us)
/* probe abort: a request unanswered this long (device ns) is re-issued */
#define HPFT_PROBE_ABORT_NS (300000u)

/* ======================= DCQCN, one state per QP =======================
 * The firmware's reaction point, parameters read from this host's
 * ecn/roce_rp defaults (rpg_time_reset 300 us, rpg_byte_reset 32767 B,
 * rpg_threshold 1, ai 5 Mb/s, hai 50 Mb/s, min_dec_fac 50 %, monitor 4 us,
 * g = 5/1024, alpha timer 55 us, initial alpha 1). alpha is fxp16; rates are
 * the device's fxp20 units, 2^20 = line rate. Every parameter is a mailbox
 * knob (0xcce <value> <which>): 0 AI, 1 HAI, 2 rate timer, 3 F, 8 loss cut,
 * 9 loss gap, 16 g, 17 alpha timer, 18 byte counter, 19 monitor period.
 * The state machine itself is unchanged from the version validated on
 * 2026-09-05; what changed is that it is now driven by exactly the events
 * of its own QP. */
#define DQ_ALPHA_ONE (65536u)
static volatile uint32_t g_dq_ai         = 26u;
static volatile uint32_t g_dq_hai        = 262u;
static volatile uint32_t g_dq_time_us    = 300u;
static volatile uint32_t g_dq_f          = 1u;
static volatile uint32_t g_dq_g16        = 320u;
static volatile uint32_t g_dq_alpha_us   = 55u;
static volatile uint32_t g_dq_bytes      = 32767u;
static volatile uint32_t g_dq_monitor_us = 4u;
static volatile uint32_t g_dq_loss_mdf16 = 32768u;
static volatile uint32_t g_dq_loss_gap_us = 300u;
/* 0xcce <0|1> 10: after a loss the target rate Rt is reset to the cut rate
 * too (a "slow restart": recovery climbs additively from where the cut
 * left the QP), instead of staying at the pre-loss rate so that the fast
 * recovery stages jump straight back to it. */
static volatile uint32_t g_dq_loss_rt_reset = 1u;
/* 0xcce <0|1> 11: the firmware's clamp_tgt_rate / clamp_tgt_rate_after_time_inc
 * pair. 0 = clamp the target rate Rt to Rc on every CNP (the paper's
 * "clamp target rate"); 1 = clamp it only when the last increase was a
 * timer increase (the host PF here reads clamp_tgt_rate=0 and
 * clamp_tgt_rate_after_time_inc=1), otherwise Rt keeps its value and
 * the recovery stages aim back at it. */
static volatile uint32_t g_dq_clamp_mode;
/* Default 1, matched against the firmware DCQCN (UPCC=0) on 2026-09-07:
 * three senders into one 50 G drop-metered VF give 47.05 G on firmware
 * and 46.8 G here with the reset (15.4-15.8 per flow set, wire 50.7 G);
 * without it the QPs jump back to the pre-loss rate within a few stages,
 * keep overrunning the meter and read 31.6 G with ~40 % retransmissions.
 * The card's own roce_slow_restart_en is 1. */

static volatile uint32_t g_dq_gpow[12];
static inline void hpft_dq_gpow_init(void)
{
	uint32_t f = 65536u - g_dq_g16;

	for (int i = 0; i < 12; i++) {
		g_dq_gpow[i] = f;
		f = (uint32_t)(((uint64_t)f * f) >> 16);
	}
}

typedef struct {
	volatile uint32_t rc;      /* current rate Rc, fxp20 */
	volatile uint32_t rt;      /* target rate Rt */
	volatile uint32_t alpha;   /* fxp16 */
	volatile uint32_t st_t;    /* timer stage */
	volatile uint32_t st_b;    /* byte stage */
	volatile uint32_t t_rate;
	volatile uint32_t t_alpha;
	volatile uint32_t t_cut;
	volatile uint32_t t_loss;
	volatile uint32_t bytes;
	volatile uint32_t cnp_f;
	volatile uint32_t inc_timer;   /* 1: the last increase was a timer increase */
} hpft_dq_t;

static inline void hpft_dq_reset(volatile hpft_dq_t *q, uint32_t now)
{
	q->rc = DOCA_PCC_DEV_MAX_RATE;
	q->rt = DOCA_PCC_DEV_MAX_RATE;
	q->alpha = DQ_ALPHA_ONE;
	q->st_t = 0;
	q->st_b = 0;
	q->t_rate = now;
	q->t_alpha = now;
	q->t_cut = now - g_dq_monitor_us;
	q->t_loss = now - g_dq_loss_gap_us;
	q->bytes = 0;
	q->cnp_f = 0;
	q->inc_timer = 0;
}

static inline void hpft_dq_step(volatile hpft_dq_t *q)
{
	uint32_t rt = q->rt, rc = q->rc, f = g_dq_f;

	if (rt < rc)
		rt = rc;
	if (q->st_t > f && q->st_b > f)
		rt += g_dq_hai;
	else if (q->st_t > f || q->st_b > f)
		rt += g_dq_ai;
	if (rt > DOCA_PCC_DEV_MAX_RATE)
		rt = DOCA_PCC_DEV_MAX_RATE;
	rc = (rt >> 1) + (rc >> 1);
	q->rt = rt;
	q->rc = rc > DOCA_PCC_DEV_MAX_RATE ? DOCA_PCC_DEV_MAX_RATE : rc;
}

static inline void hpft_dq_advance(volatile hpft_dq_t *q, uint32_t now)
{
	uint32_t per = g_dq_alpha_us ? g_dq_alpha_us : 1u;
	uint32_t el = now - q->t_alpha;

	if (el >= per) {
		uint32_t k = el / per, a = q->alpha;

		if (!g_dq_gpow[0])
			hpft_dq_gpow_init();
		if (q->cnp_f) {
			a += (uint32_t)(((uint64_t)(DQ_ALPHA_ONE - a) * g_dq_g16) >> 16);
			if (a > DQ_ALPHA_ONE)
				a = DQ_ALPHA_ONE;
			q->cnp_f = 0;
			k--;
		}
		if (k >= 4096u) {
			a = 0;
		} else {
			for (int i = 0; k && i < 12; i++, k >>= 1)
				if (k & 1u)
					a = (uint32_t)(((uint64_t)a * g_dq_gpow[i]) >> 16);
		}
		q->alpha = a;
		q->t_alpha = now - (el % per);
	}
	per = g_dq_time_us ? g_dq_time_us : 1u;
	el = now - q->t_rate;
	if (el >= per) {
		uint32_t k = el / per;

		if (k > 64u)
			k = 64u;
		while (k--) {
			q->st_t++;
			hpft_dq_step(q);
		}
		q->inc_timer = 1;
		q->t_rate = now - (el % per);
	}
}

static inline void hpft_dq_bytes(volatile hpft_dq_t *q, uint32_t nbytes, uint32_t now)
{
	uint32_t br = g_dq_bytes ? g_dq_bytes : 1u;
	uint32_t nb = q->bytes + nbytes;
	uint32_t k = nb / br;

	hpft_dq_advance(q, now);
	if (k > 64u)
		k = 64u;
	while (k--) {
		q->st_b++;
		hpft_dq_step(q);
		q->inc_timer = 0;
	}
	q->bytes = nb % br;
}

static inline void hpft_dq_cnp(volatile hpft_dq_t *q, uint32_t now)
{
	hpft_dq_advance(q, now);
	q->cnp_f = 1;
	if ((uint32_t)(now - q->t_cut) >= g_dq_monitor_us) {
		uint32_t rc = q->rc;
		uint32_t cut = (uint32_t)(((uint64_t)rc * q->alpha) >> 17);
		uint32_t max = (uint32_t)(((uint64_t)rc * (65536u - g_dq_loss_mdf16)) >> 16);

		if (cut > max)
			cut = max;
		if (!g_dq_clamp_mode || q->inc_timer)
			q->rt = rc;
		q->rc = (rc > cut + HPFT_MIN_RATE) ? rc - cut : HPFT_MIN_RATE;
		q->st_t = 0;
		q->st_b = 0;
		q->bytes = 0;
		q->t_rate = now;
		q->t_cut = now;
	}
}

static inline int hpft_dq_loss(volatile hpft_dq_t *q, uint32_t now)
{
	uint32_t rc, nr;

	if (g_dq_loss_mdf16 >= 65536u ||
	    (uint32_t)(now - q->t_loss) < g_dq_loss_gap_us)
		return 0;
	hpft_dq_advance(q, now);
	rc = q->rc;
	nr = (uint32_t)(((uint64_t)rc * g_dq_loss_mdf16) >> 16);
	q->rc = nr > HPFT_MIN_RATE ? nr : HPFT_MIN_RATE;
	q->rt = g_dq_loss_rt_reset ? q->rc : rc;
	q->st_t = 0;
	q->st_b = 0;
	q->bytes = 0;
	q->t_rate = now;
	q->t_loss = now;
	return 1;
}

/* ===================== Swift, one window per QP =====================
 * Kumar et al., SIGCOMM'20, fabric-delay half only (RDMA has no host-side
 * endpoint delay to subtract) and delay-only (CNPs ignored). The window is
 * this QP's own; before the SOURCE_QP change a whole VF pair shared one
 * window and the port sat at 88 %, while the vendor's per-QP build reached
 * 98 %. Thresholds are device-clock ns measured on THIS fabric. */
#define SW_PKT       (1024u)
#define SW_MIN_CWND  (SW_PKT)
#define SW_MAX_CWND  ((1u << 20) - SW_PKT)
#define SW_INIT_CWND (64u << 10)
/* Tuned on this fabric 2026-09-07 following the paper's own procedure
 * (section 3.5 and 5.1: pick the base target at the throughput knee, use
 * flow scaling for fairness). The base RTT here is ~3 us and the BDP per
 * QP with 32 QPs on a 200 G port is ~3 packets, so the paper's increment
 * of one packet per RTT is a third of a window and overshoots; 256 B per
 * RTT with a 40 us flow-scaling range gives 173.4 G of the 177.2 G
 * ceiling, Jain 1.000 across eight flow sets and ~2e5 marked frames per
 * 12 s (the paper's 1 packet / 20 us: 169 G, Jain 0.993, 5e6 marks).
 * Base target 25 us, beta 0.8 and max_mdf 0.5 stay as in the paper. */
static volatile uint32_t g_sw_base_target = 25000u;
static volatile uint32_t g_sw_fs_range    = 40000u;
static volatile uint32_t g_sw_alpha_ns    = 50000u;
static volatile uint32_t g_sw_beta_ns     = 5000u;
static volatile uint32_t g_sw_ai          = (SW_PKT / 4);
static volatile uint32_t g_sw_beta16      = 52429u;   /* b = 0.8 */
static volatile uint32_t g_sw_max_mdf16   = 32768u;   /* max_mdf = 0.5 */
static volatile uint32_t g_sw_use_srtt    = 1u;
/* 0xcd1 <0|1> 8: which RTT turns the window into the pacing rate. 1 = the
 * smoothed RTT (lags the queue both ways: pacing runs ahead of the window
 * while the queue builds and behind it while the queue drains, which
 * deepens the trough where the link idles); 0 = the latest sample, the
 * closest thing to a window's own self-clocking. */
static volatile uint32_t g_sw_rate_srtt   = 2u;

static inline uint32_t hpft_isqrt(uint32_t x)
{
	uint32_t r = 0, b = 1u << 30;

	while (b > x)
		b >>= 2;
	while (b) {
		if (x >= r + b) {
			x -= r + b;
			r = (r >> 1) + b;
		} else {
			r >>= 1;
		}
		b >>= 2;
	}
	return r;
}

/* ZTR-RTTCC, the vendor reference rate rule, one rate per QP */
#define ZTR_UPDATE_FACTOR (((1u << 16) * 10u) / 100u)
#define ZTR_AI            (((1u << 20) * 5u) / 100u)
#define ZTR_BASE_RTT      (7000u)
#define ZTR_MAX_DELAY     (70000u)
#define ZTR_MIN_RATE      (1u << (20 - 14))
#define ZTR_DEC_FACTOR      ((1u << 16) - ZTR_UPDATE_FACTOR)
#define ZTR_CNP_DEC_FACTOR  ((1u << 16) - 2u * ZTR_UPDATE_FACTOR)
#define ZTR_NACK_DEC_FACTOR ((1u << 16) - 5u * ZTR_UPDATE_FACTOR)

/* ========================= per-QP record ========================= */
typedef struct {
	volatile uint32_t gen;       /* 0 = free; matched against the context */
	volatile uint32_t key;       /* hpft_bind_key(vhca, qpn) this record is for */
	volatile uint32_t qpn;
	volatile uint32_t vhca;      /* the function (VF) this QP belongs to */
	volatile uint32_t set;       /* flow-set index + 1; 0 = not known yet */
	volatile uint32_t last_ts;

	hpft_dq_t dq;                /* DCQCN */

	volatile uint32_t sw_cwnd;   /* Swift window, bytes */
	volatile uint32_t sw_rtt_s;  /* Swift smoothed RTT, device ns */
	volatile uint32_t sw_last_dec;
	volatile uint32_t sw_target;

	volatile uint32_t cc_rate;   /* ZTR / AIMD rate, fxp20 */
	volatile uint32_t flags;     /* bit0 was_cnp, bit1 was_nack */

	volatile uint32_t rtt_last;
	volatile uint32_t rtt_min;
	volatile uint32_t rtt_n;
	/* probe state machine (the vendor template's): one probe in flight,
	 * re-requested when it was evidently lost */
	volatile uint32_t pr_inflight;   /* a request has left on the wire */
	volatile uint32_t pr_start;      /* event timestamp it left at */
	volatile uint32_t pr_pending;    /* TX events since a request with none sent */
	volatile uint32_t pr_abort;      /* consecutive aborts (timeout doubles) */

	volatile uint32_t paced;     /* last rate written to the wire */
	/* Time-weighted account of what this QP was really paced at. The
	 * readback used to report `paced` itself - the last value written,
	 * sampled once a second - which is an instantaneous sample of a
	 * quantity that moves every few microseconds: the wire carried 1.7 to
	 * 24.5 % more than the sum of those samples claimed had been
	 * programmed, while the receiver's ledger agreed with the wire to
	 * within 1 % (V1, 2026-09-09). These accumulate rate x time; the
	 * readback hands the total to the host, which divides by the interval
	 * between its own two queries. Scaled by 2^14 so four QPs at line rate
	 * for a second stay inside 32 bits. */
	volatile uint32_t pacc;      /* sum of paced x us, >> 14 */
	volatile uint32_t pacc_ts;   /* when the account was last brought up to date */
	volatile uint32_t tok_ts;    /* token law: when this QP last paid the pool */
	volatile uint32_t epoch_ts;

	volatile uint32_t n_cnp;
	volatile uint32_t n_nack;
	volatile uint32_t n_tx;
	volatile uint32_t avg_b32_x16;
} hpft_q_t;

static hpft_q_t g_q[HPFT_QSLOTS];
static volatile uint32_t g_q_next;   /* cursor over the spare slots */
static volatile uint32_t g_gen = 1;
/* A QP's record lives at a slot derived from its own key, so the record
 * table is its own index. The framework's algo context is the fast path to
 * the record, but it is not one per QP: measured 2026-09-08, two QPs of
 * different VFs (vf1 qpn 578 and vf3 qpn 294 on hpft-dpu) took turns in ONE
 * context, and QPs of equal number on different VFs did the same. So a
 * context that turns up with another QP's number is a cache miss and the
 * record is found here instead.
 *
 * Placement is a function of the key rather than of a shared cursor because
 * the framework fans events over sixteen DPA threads and the DPA offers no
 * atomic to arbitrate a cursor with. Handing out slots from a shared counter
 * let two threads take the SAME slot for two different QPs, and two threads
 * take TWO slots for the same QP: measured 2026-09-09 over six V2 runs, one
 * QP held two records while another VF's QP held none, so that QP's flow set
 * was short a member every time. Probing from the key's own home slot makes
 * the race idempotent - two threads racing on one QP land on one record. */
static inline uint32_t hpft_q_home(uint32_t key)
{
	/* the TOP bits of the multiplication, not the low ones: HPFT_QHOME is
	 * a power of two, so a modulo would keep the low bits, and those
	 * depend only on the low bits of the key. Keys differing above the
	 * seventh bit would then all share one home slot - (vhca 26, qpn
	 * 0x162), (27, 0x262) and (25, 0x1e2) all landed on slot 5. */
	return ((key + 1u) * 2246822519u) >> 25;
}

/* ========================= per-flow-set ========================= */
typedef struct {
	volatile uint32_t id;        /* set id chosen by the sender agent; 0 free */
	volatile uint32_t budget;    /* R, fxp20 */
	volatile uint32_t sum_cc;    /* sum of c_j over the QPs drawing tokens */
	volatile uint32_t nq;
	volatile uint32_t qslot[HPFT_SET_QPS];
	volatile uint32_t epoch_ts;
	volatile uint32_t nlive;     /* QPs counted in sum_cc */
	volatile int32_t  tok;       /* token law: pool level, bytes; may go negative */
	volatile uint32_t tok_ts;    /* token law: last refill, us */
} hpft_set_t;


static hpft_set_t g_set[HPFT_SETS];

/* {QP -> set id} learned from the sender agent (mailbox 0xb48e). A QP is
 * named by (vhca_id, qpn), packed as vhca_id << 24 | qpn: QP numbers are
 * unique per FUNCTION only, and eight VFs on one host each hand them out
 * from their own allocator, so two VFs can own the same number at the same
 * time (measured 2026-09-07: vf0 held 546..549 while vf1 held 470..473 --
 * neighbours, not equal, that day). The event carries the function as
 * vhca_id (dword 1 of the event, bits 16..31 in the little-endian view);
 * the agent learns each VF's vhca_id from the DPU with vhca_of. An agent
 * that has no such table sends the bare qpn as the key, and the lookup
 * falls back to that. */
#define HPFT_MAP_SIZE (2048)
static volatile uint32_t g_map_qpn[HPFT_MAP_SIZE];
static volatile uint32_t g_map_set[HPFT_MAP_SIZE];

static inline uint32_t hpft_map_hash(uint32_t key)
{
	/* top bits, for the reason in hpft_q_home */
	return ((key + 1u) * 2654435761u) >> 21;
}

static inline uint32_t hpft_bind_key(uint32_t vhca, uint32_t qpn)
{
	return ((vhca & 0xffu) << 24) | (qpn & 0xffffffu);
}

/* vhca_id of the function that owns this event's QP */
static inline uint32_t hpft_ev_vhca(doca_pcc_dev_event_t *event)
{
	uint32_t v = __builtin_bswap32(((volatile uint32_t *)event)[1]);

	return v >> 16;
}

static inline uint32_t hpft_map_get(uint32_t qpn)
{
	uint32_t h = hpft_map_hash(qpn);

	for (uint32_t pr = 0; pr < 8u; pr++) {
		uint32_t i = (h + pr) % HPFT_MAP_SIZE;

		if (g_map_qpn[i] == qpn + 1u)
			return g_map_set[i];
		if (g_map_qpn[i] == 0)
			return 0;
	}
	return 0;
}

static inline void hpft_map_put(uint32_t qpn, uint32_t set_id)
{
	uint32_t h = hpft_map_hash(qpn);

	for (uint32_t pr = 0; pr < 8u; pr++) {
		uint32_t i = (h + pr) % HPFT_MAP_SIZE;

		if (g_map_qpn[i] == qpn + 1u || g_map_qpn[i] == 0) {
			/* value first, key last: the reader matches on the key,
			 * so publishing the key first lets it pair a new key
			 * with whatever value the slot held before. Read that
			 * way, a stale set id makes hpft_q_reassert unbind a
			 * QP that was bound correctly. */
			g_map_set[i] = set_id;
			g_map_qpn[i] = qpn + 1u;
			return;
		}
	}
}

/* A flow set's slot is a function of its id, for the same reason a QP's
 * record is a function of its key (hpft_q_home): the framework runs the
 * algorithm on sixteen DPA threads at once and the DPA has no atomic to
 * arbitrate a shared allocator with. Taking "the first free slot" lets two
 * threads claiming two DIFFERENT ids pick the SAME slot; one id is then
 * lost, and its QPs sit in a slot carrying another set's id and budget
 * until the next millisecond's re-assert unbinds them and they claim a
 * slot further along - by which time the id can hold two slots, the second
 * one with no budget, and the QPs in it run at the unknown-flow allowance
 * for the rest of the run (measured on V8, four runs of four: one 24-QP
 * flow set held 22 or 23 of its QPs). Placing by the id makes the race
 * idempotent: every thread claiming one id walks the same probe order and
 * lands on the same slot. */
static inline uint32_t hpft_set_home(uint32_t id)
{
	/* the TOP bits of the multiplication, as in hpft_q_home: HPFT_SETS is
	 * a power of two, so a modulo would keep only the low bits of the id */
	return (((id + 1u) * 2246822519u) >> 27) % HPFT_SETS;
}

/* Where a set id lives, or -1. Lookup only: it never claims, and it stops
 * at the first empty slot the way any open-addressed lookup does. */
static inline int hpft_set_find(uint32_t id)
{
	uint32_t h;

	if (!id)
		return -1;
	h = hpft_set_home(id);
	for (uint32_t pr = 0; pr < HPFT_SETS; pr++) {
		uint32_t i = (h + pr) % HPFT_SETS;

		if (g_set[i].id == id)
			return (int)i;
		if (!g_set[i].id)
			return -1;
	}
	return -1;
}

static inline int hpft_set_of(uint32_t id)
{
	uint32_t h;

	if (!id)
		return -1;
	h = hpft_set_home(id);
	/* the whole table, starting at the id's own home: a set never has to
	 * give up while a slot is free anywhere */
	for (uint32_t pr = 0; pr < HPFT_SETS; pr++) {
		uint32_t i = (h + pr) % HPFT_SETS;
		volatile hpft_set_t *s = &g_set[i];

		if (s->id == id)
			return (int)i;
		if (!s->id) {
			/* the slot is emptied before the id publishes it, so a
			 * thread that sees the id sees a slot that is ready:
			 * publishing first lets this initialiser wipe the
			 * budget the mailbox has just written, or the members
			 * another thread has just added */
			s->budget = 0;
			s->sum_cc = 0;
			s->nq = 0;
			s->nlive = 0;
			s->tok = 0;
			s->tok_ts = 0;
			s->epoch_ts = 0;
			s->id = id;
			return (int)i;
		}
	}
	return -1;
}

/* ========================= diagnostics ========================= */
static volatile uint32_t g_ev_tx, g_ev_cnp, g_ev_nack, g_ev_rtt;
static volatile uint32_t g_q_alloc, g_q_bound;
/* record re-inits (a context turned up holding another QP's number) and
 * unbinds, the latter counted apart by reason: the map disagreeing with the
 * record, and the QP having gone quiet (0xdf3) */
static volatile uint32_t g_q_reinit, g_q_unbind, g_unbind_map, g_unbind_quiet;

static inline void hpft_q_init(volatile hpft_q_t *q, uint32_t now)
{
	q->key = 0;
	q->pacc = 0;
	q->pacc_ts = now;
	q->qpn = 0;
	q->vhca = 0;
	q->set = 0;
	q->last_ts = now;
	hpft_dq_reset(&q->dq, now);
	q->sw_cwnd = SW_INIT_CWND;
	q->sw_rtt_s = 0;
	q->sw_last_dec = now;
	q->sw_target = 0;
	q->cc_rate = DOCA_PCC_DEV_MAX_RATE;
	q->flags = 0;
	q->rtt_last = 0;
	q->rtt_min = 0;
	q->rtt_n = 0;
	q->pr_inflight = 0;
	q->pr_start = now;
	q->pr_pending = 1;
	q->pr_abort = 0;
	q->paced = 0;
	q->epoch_ts = now;
	q->n_cnp = 0;
	q->n_nack = 0;
	q->n_tx = 0;
	q->avg_b32_x16 = 34 * 16;
}

/* A slot outside the keyed window, for a record that cannot be placed by key:
 * an event with no QP number of its own, or the rare key whose eight probes
 * are all held by live QPs of other keys. */
static inline uint32_t hpft_q_spare(void)
{
	uint32_t i = g_q_next;

	if (i < HPFT_QHOME || i >= HPFT_QSLOTS)
		i = HPFT_QHOME;
	g_q_next = (i + 1u >= HPFT_QSLOTS) ? HPFT_QHOME : i + 1u;
	return i;
}

/* Find this key's record, or take a slot for it. Only ever called with a
 * real key; an event carrying no QP number of its own has nothing to key a
 * record by and takes a spare slot instead. */
static inline uint32_t hpft_q_claim(uint32_t key, uint32_t qpn, uint32_t vhca,
				   uint32_t now)
{
	uint32_t h = hpft_q_home(key);

	for (uint32_t pr = 0; pr < 8u; pr++) {
		uint32_t i = (h + pr) % HPFT_QHOME;
		volatile hpft_q_t *q = &g_q[i];

		if (q->gen && q->key == key)
			return i;                      /* already ours */
		/* free, or left by a QP gone far longer than the two seconds
		 * after which a QP lets go of its set */
		if (!q->gen || (uint32_t)(now - q->last_ts) > HPFT_QP_GONE_US) {
			hpft_q_init(q, now);
			/* who the record is for, written before gen publishes
			 * it: a thread that loses the slot to another key must
			 * not be able to stamp its own QP number on the
			 * winner's record afterwards */
			q->key = key;
			q->qpn = qpn;
			q->vhca = vhca;
			q->gen = g_gen ? g_gen : 1u;
			g_gen = q->gen + 1u;
			g_q_alloc++;
			return i;
		}
	}
	/* every probe held by a live QP of another key */
	{
		uint32_t i = hpft_q_spare();

		hpft_q_init(&g_q[i], now);
		g_q[i].key = key;
		g_q[i].qpn = qpn;
		g_q[i].vhca = vhca;
		g_q[i].gen = g_gen ? g_gen : 1u;
		g_gen = g_q[i].gen + 1u;
		g_q_alloc++;
		return i;
	}
}

/* One Swift step per RTT sample, on this QP's own window. */
static inline void hpft_swift_step(volatile hpft_q_t *q, uint32_t rtt, uint32_t ts)
{
	uint32_t cwnd = q->sw_cwnd;
	uint32_t rs = q->sw_rtt_s;
	uint32_t target, fs, s;

	if (rs == 0)
		rs = rtt;
	else
		rs = rs + (uint32_t)(((int32_t)(rtt - rs)) >> 2);
	q->sw_rtt_s = rs;

	s = hpft_isqrt(cwnd >> 2);          /* 16 * sqrt(cwnd / 1024) */
	if (s == 0) {
		fs = g_sw_fs_range;
	} else {
		uint32_t a = (g_sw_alpha_ns << 4) / s;

		fs = (a > g_sw_beta_ns) ? (a - g_sw_beta_ns) : 0;
		if (fs > g_sw_fs_range)
			fs = g_sw_fs_range;
	}
	target = g_sw_base_target + fs;
	q->sw_target = target;
	if (g_sw_use_srtt)
		rtt = rs;
	{
		int can_dec = (uint32_t)(ts - q->sw_last_dec) >= rs;

		if ((q->flags & 2u) && can_dec) {
			cwnd = (uint32_t)(((uint64_t)cwnd * ((1u << 16) - g_sw_max_mdf16)) >> 16);
			q->flags &= ~2u;
			q->sw_last_dec = ts;
		} else if (rtt < target) {
			cwnd += g_sw_ai;
			q->flags &= ~2u;
		} else if (can_dec) {
			uint32_t diff = rtt - target;
			uint32_t mdf;

			if (diff > (1u << 21))
				diff = 1u << 21;
			mdf = (diff << 10) / ((rtt >> 6) ? (rtt >> 6) : 1u);
			mdf = (uint32_t)(((uint64_t)mdf * g_sw_beta16) >> 16);
			if (mdf > g_sw_max_mdf16)
				mdf = g_sw_max_mdf16;
			cwnd = (uint32_t)(((uint64_t)cwnd * ((1u << 16) - mdf)) >> 16);
			q->sw_last_dec = ts;
		}
	}
	if (cwnd < SW_MIN_CWND)
		cwnd = SW_MIN_CWND;
	if (cwnd > SW_MAX_CWND)
		cwnd = SW_MAX_CWND;
	q->sw_cwnd = cwnd;
}

/* The rate this QP's CC would put on the wire on its own. For Swift that is
 * the window over its own smoothed RTT; 2^20 units, 25 B/ns at 200 G. */
static inline uint32_t hpft_cc_rate(volatile hpft_q_t *q)
{
	if (g_algo == HPFT_CC_SWIFT) {
		/* 0xcd1 <n> 8: which RTT turns the window into the pacing rate.
		 * 0 latest sample, 1 smoothed, 2 the larger of the two (the lower
		 * of the two rates: a low probe sample never turns into a burst,
		 * a high one slows the QP at once), 3 the smaller of the two. */
		uint32_t rs;
		uint64_t r;

		if (g_sw_rate_srtt == 1u)
			rs = q->sw_rtt_s;
		else if (g_sw_rate_srtt == 2u)
			rs = q->sw_rtt_s > q->rtt_last ? q->sw_rtt_s : q->rtt_last;
		else if (g_sw_rate_srtt == 3u)
			rs = (q->sw_rtt_s && q->sw_rtt_s < q->rtt_last) ? q->sw_rtt_s : q->rtt_last;
		else
			rs = q->rtt_last;

		if (!rs)
			rs = q->sw_rtt_s ? q->sw_rtt_s : (q->rtt_last ? q->rtt_last : 3000u);
		r = ((uint64_t)q->sw_cwnd << 20) / (25ull * rs);

		if (r > DOCA_PCC_DEV_MAX_RATE)
			r = DOCA_PCC_DEV_MAX_RATE;
		return (uint32_t)(r ? r : HPFT_MIN_RATE);
	}
	if (g_algo == HPFT_CC_DCQCN)
		return q->dq.rc;
	return q->cc_rate;
}

/* per-set 1 ms housekeeping: sum c_j over the QPs drawing tokens.
 *
 * The member list is NEVER compacted and its length never shrinks: a
 * departed QP's entry is retired in place. Compaction is what made this
 * wrong before -- the epoch rewrote qslot[] and nq while another DPA thread
 * was appending a newly bound QP, so an append could be lost, the QP
 * vanished from the denominator, and the whole flow set then sent
 * N/(N-1) of its rate (measured 2026-09-06 on the V1 shape: flow sets 16 %
 * over their share while the port was already full). Losses are now
 * self-healing instead of permanent, because every bound QP re-asserts its
 * membership once per millisecond (the per-QP epoch in the handler). */
static inline void hpft_set_epoch(volatile hpft_set_t *s, uint32_t now, uint32_t sidx)
{
	uint32_t sum = 0, live = 0, n = s->nq;
	uint32_t elapsed = (uint32_t)(now - s->epoch_ts);

	if (elapsed < HPFT_EPOCH_US)
		return;
	s->epoch_ts = now;
	if (elapsed > HPFT_EPOCH_US * 4u)
		elapsed = HPFT_EPOCH_US * 4u;   /* the set was idle */
	if (n > HPFT_SET_QPS)
		n = HPFT_SET_QPS;
	for (uint32_t k = 0; k < n; k++) {
		uint32_t si = s->qslot[k];
		volatile hpft_q_t *q;

		if (si >= HPFT_QSLOTS)
			continue;              /* retired or never written */
		q = &g_q[si];
		if (!q->gen || q->set != sidx + 1u ||
		    (uint32_t)(now - q->last_ts) > HPFT_QP_STALE_US) {
			/* gone, or an entry a lost append left pointing at a QP
			 * that belongs to another set */
			s->qslot[k] = HPFT_SET_EMPTY;
			continue;
		}
		if ((uint32_t)(now - q->last_ts) > HPFT_ACTIVE_US)
			continue;              /* not drawing: not counted */
		sum += hpft_cc_rate(q);
		live++;
	}
	s->sum_cc = sum;
	s->nlive = live;
}

/* Charge this QP's account for the stretch it has just spent at its current
 * rate. Called only from the event path: the mailbox handler runs on another
 * clock, and settling against that one scattered the answer from 0.91 to 1.58
 * of the wire. What is therefore left out of a readback is the stretch since
 * the QP's last event, tens of microseconds against a sampling interval of a
 * second. */
static inline void hpft_pacc_settle(volatile hpft_q_t *q, uint32_t now)
{
	uint32_t d = (uint32_t)(now - q->pacc_ts);

	if (!d)
		return;
	if (d > HPFT_PACC_MAX_US)
		d = HPFT_PACC_MAX_US;      /* the QP was away: do not invent traffic */
	q->pacc_ts = now;
	q->pacc += (uint32_t)(((uint64_t)q->paced * d) >> 14);
}

/* Refill the pool at R and cap it at one control period's worth: a set may
 * burst up to the depth and no more, which is the classic bucket and the
 * same time constant the rest of the executor already runs on. */
static inline void hpft_pool_refill(volatile hpft_set_t *s, uint32_t now)
{
	uint32_t dt = (uint32_t)(now - s->tok_ts);
	int64_t depth, v;

	if (!s->budget)
		return;
	if (dt > HPFT_EPOCH_US)
		dt = HPFT_EPOCH_US;         /* first event, or the set idled */
	if (!dt)
		return;
	s->tok_ts = now;
	depth = (((int64_t)s->budget * HPFT_LINE_B_PER_US * HPFT_EPOCH_US) >> 20) / 4;
	v = (int64_t)s->tok + (((int64_t)s->budget * HPFT_LINE_B_PER_US * dt) >> 20);
	/* The debt is bounded like the credit. Without a floor a transient
	 * over-grant drives the pool arbitrarily negative and every QP of the
	 * set sits at the rate floor until the refill has paid it all back:
	 * measured on V2, RDMA flow-sets fell to 1.6 G against a 23 G share
	 * while the unshaped TCP half took 35 G. */
	if (v > depth)
		v = depth;
	else if (v < -depth)
		v = -depth;
	s->tok = (int32_t)v;
}

/* What one QP may send: its equal share of R, plus whatever the pool has
 * spare. The pool carries the outstanding reservations of every QP in the
 * set (each reserves the bytes its current rate will send before its next
 * event, and gets refunded what the wire did not carry), so "spare" means
 * the set as a whole is under its budget - which is the only condition under
 * which one QP may exceed its equal share. Both terms are rates in the
 * device's fxp20; the spare term is the pool spread over one control
 * period. */
static inline uint32_t hpft_pool_ceiling(volatile hpft_set_t *s)
{
	uint32_t n = s->nlive ? s->nlive : (s->nq ? s->nq : 1u);
	int64_t bonus = ((int64_t)s->tok << 20) /
			((int64_t)HPFT_EPOCH_US * HPFT_LINE_B_PER_US);
	int64_t c = ((int64_t)s->budget + bonus) / n;

	if (c < (int64_t)HPFT_MIN_RATE)
		c = HPFT_MIN_RATE;
	if (c > (int64_t)s->budget)
		c = s->budget;
	return (uint32_t)c;
}

static inline void hpft_set_add(volatile hpft_set_t *s, uint32_t slot)
{
	uint32_t n = s->nq, free_k = HPFT_SET_QPS;

	if (n > HPFT_SET_QPS)
		n = HPFT_SET_QPS;
	for (uint32_t k = 0; k < n; k++) {
		if (s->qslot[k] == slot)
			return;                /* already a member */
		if (free_k == HPFT_SET_QPS && s->qslot[k] == HPFT_SET_EMPTY)
			free_k = k;
	}
	if (free_k < HPFT_SET_QPS) {
		s->qslot[free_k] = slot;
		return;
	}
	if (n < HPFT_SET_QPS) {
		s->qslot[n] = slot;
		s->nq = n + 1;             /* a lost race is repaired next epoch */
	}
}
/* Re-assert this QP's membership of its flow set. Cheap (a scan of at most
 * HPFT_SET_QPS words once per millisecond) and it is what makes a lost
 * append harmless. */
static inline void hpft_q_reassert(volatile hpft_q_t *q, uint32_t slot)
{
	int si = (int)q->set - 1;

	if (si >= 0 && si < HPFT_SETS) {
		/* Once a millisecond, follow the map: a QP sitting anywhere
		 * other than the slot its set id actually lives in lets go
		 * and is bound again on its next TX event. That covers both
		 * the agent correcting a binding after the fact (the
		 * resolver's first answer for a QP can be a stale pairing
		 * from the previous run) and a slot claimed out from under
		 * this QP by another id - and, because the surviving slot is
		 * always the one nearest the id's home, it also empties a
		 * slot two threads made for one id. */
		uint32_t id = hpft_map_get(q->key);

		if (!id)
			id = hpft_map_get(q->qpn);
		if (id && hpft_set_find(id) != si) {
			q->set = 0;
			g_q_unbind++;
			g_unbind_map++;
			return;
		}
		hpft_set_add(&g_set[si], slot);
	}
}

/*
 * Main entry point to user CC algorithm.
 *
 * @algo_ctxt [in]: this QP's own 12 dwords, restored by the framework.
 * @event [in]: the event.
 * @attr [in]: algo type.
 * @results [out]: the rate to program.
 */
/* Every event of every flow, whatever algo_slot the framework tags it
 * with: about 96 % of the data QPs' events arrive with slot 15 (the slot the
 * firmware keeps for its built-in CC), and handing those to the framework's
 * default algorithm would hand our data QPs to the firmware's own CC. */
void doca_pcc_dev_user_algo(doca_pcc_dev_algo_ctxt_t *algo_ctxt,
			    doca_pcc_dev_event_t *event,
			    const doca_pcc_dev_attr_t *attr,
			    doca_pcc_dev_results_t *results)
{
	doca_pcc_dev_event_general_attr_t a = doca_pcc_dev_get_ev_attr(event);
	uint32_t now;
	volatile hpft_q_t *q;
	uint32_t slot, want_rtt = 0, r;
	int ctx_ok;
	volatile uint32_t *cx = (volatile uint32_t *)algo_ctxt;

	(void)attr;
	results->rtt_req = 0;
	now = doca_pcc_dev_get_timer_lo();

	/* ---- this QP's record ----
	 * The framework's algo context is the attribution the hardware gives:
	 * a CNP, NACK or RTT event carries no usable QP number of its own (its
	 * qpn word reads 1, the function's GSI QP, measured 2026-09-08), so
	 * for those the context's record is the QP's record. A TX event does
	 * carry its QP number, and a context is not strictly one per QP
	 * (two data QPs of different VFs took turns in one context), so a TX
	 * event whose number is not the context's is a cache miss and the
	 * record is claimed from the key's own home slot; the context then
	 * points at that record until the other QP's next TX event. */
	{
		uint32_t key = 0, eq = 0, ev = 0;

		ctx_ok = (cx[0] == HPFT_CTX_MAGIC && cx[1] < HPFT_QSLOTS && cx[2] &&
			  g_q[cx[1]].gen == cx[2]);

		slot = cx[1];
		if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_TX) {
			eq = doca_pcc_dev_get_flow_qpn(event);
			ev = hpft_ev_vhca(event);
			key = hpft_bind_key(ev, eq);
		}
		if (ctx_ok && (!key || g_q[slot].key == key)) {
			q = &g_q[slot];
		} else {
			if (ctx_ok)
				g_q_reinit++;
			ctx_ok = 0;
			if (key) {
				slot = hpft_q_claim(key, eq, ev, now);
				q = &g_q[slot];
			} else {
				/* no QP number of its own and no usable context:
				 * nothing to key a record by, so it gets the
				 * round-robin slot and never joins a set */
				slot = hpft_q_spare();
				q = &g_q[slot];
				hpft_q_init(q, now);
				q->gen = g_gen ? g_gen : 1u;
				g_gen = q->gen + 1u;
				g_q_alloc++;
			}
			cx[0] = HPFT_CTX_MAGIC;
			cx[1] = slot;
			cx[2] = q->gen;
		}
	}
	/* A QP silent for HPFT_QP_STALE_US is treated as departed: its set
	 * has already retired it (hpft_set_epoch), and the number may by
	 * now belong to a new QP of the same function that talks to another
	 * destination (V3: vf4 after vf0). Drop the binding so the next TX
	 * event looks the QP up in the agent's map again. */
	if (q->set && (uint32_t)(now - q->last_ts) > HPFT_QP_STALE_US) {
		q->set = 0;
		g_q_unbind++;
		g_unbind_quiet++;
	}
	q->last_ts = now;
	if (g_algo == HPFT_CC_SWIFT && ctx_ok && cx[3]) {
		/* Swift's window travels with the event in the framework's
		 * context (cx[3..7]: cwnd, rtt_s, last_dec, flags, rtt_last);
		 * load it into the record for this event. Only when the context
		 * was this record's: after a cache miss the words still belong
		 * to the QP the context served before. */
		q->sw_cwnd = cx[3];
		q->sw_rtt_s = cx[4];
		q->sw_last_dec = cx[5];
		q->flags = cx[6];
		q->rtt_last = cx[7];
	}

	/* ---- events ---- */
	if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_TX) {
		doca_pcc_dev_roce_tx_cntrs_t tc = doca_pcc_dev_get_roce_tx_cntrs(event);
		uint32_t b32 = (uint32_t)tc.sent_32bytes;
		uint32_t pkts = (uint32_t)tc.sent_pkts;

		g_ev_tx++;
		q->n_tx++;
		if (!q->qpn) {
			q->qpn = doca_pcc_dev_get_flow_qpn(event);
			q->vhca = hpft_ev_vhca(event);
		}
		if (pkts > 0) {
			uint32_t avg = q->avg_b32_x16, est;

			/* the 16-bit byte counter clips at 2 MB per coalesced
			 * event; estimate from packets x a learned average */
			if (b32 < 32768u && pkts < 900u) {
				uint32_t sample = (b32 << 4) / pkts;

				avg = avg + ((int32_t)(sample - avg) >> 4);
				if (avg < 16u)
					avg = 16u;
				q->avg_b32_x16 = avg;
			}
			est = (pkts * avg) >> 4;
			if (g_algo == HPFT_CC_DCQCN)
				hpft_dq_bytes(&q->dq, est << 5, now);
		}
		/* Probe cadence for the delay-based CCs, as the vendor template
		 * does it: the request goes out with a data packet (TX flag
		 * RTT_REQ_SENT); while one is in flight nothing is asked; a
		 * request that has not left after two more TX events, or a
		 * probe that has not returned within 300 us (doubling on each
		 * abort), is asked again. Requesting only on the answer, with a
		 * 1 ms fallback, lost about half the samples on this fabric
		 * (26 K/s per QP against a 15-40 us RTT) and Swift, which
		 * grows one packet per SAMPLE, ran 4 % under the vendor build. */
		if (g_algo == HPFT_CC_SWIFT || g_algo == HPFT_CC_ZTR) {
			uint32_t ets = doca_pcc_dev_get_timestamp(event);

			if ((a.flags & DOCA_PCC_DEV_TX_FLAG_RTT_REQ_SENT) && !q->pr_inflight) {
				q->pr_inflight = 1;
				q->pr_pending = 0;
				q->pr_start = ets;
			} else {
				uint32_t el = ets - q->pr_start;
				uint32_t lim = HPFT_PROBE_ABORT_NS << (q->pr_abort > 8u ? 8u : q->pr_abort);

				if (!q->pr_inflight) {
					el = 0;
					q->pr_pending++;
				}
				if (el > lim || q->pr_pending > 2u) {
					want_rtt = 1;
					if (el > lim)
						q->pr_abort++;
					q->pr_pending = 1;
					q->pr_inflight = 0;
				}
			}
		}
		/* bind the QP to its flow set once the agent has told us */
		if (!q->set && q->qpn) {
			uint32_t id = hpft_map_get(hpft_bind_key(q->vhca, q->qpn));
			int si;

			if (!id)
				id = hpft_map_get(q->qpn);   /* agent without a vhca table */
			si = hpft_set_of(id);

			if (si >= 0) {
				q->set = (uint32_t)si + 1u;
				hpft_set_add(&g_set[si], slot);
				g_q_bound++;
			}
		}
	} else if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_CNP) {
		g_ev_cnp++;
		q->n_cnp++;
		if (g_algo == HPFT_CC_DCQCN)
			hpft_dq_cnp(&q->dq, now);
		else if (g_algo == HPFT_CC_ZTR)
			q->flags |= 1u;
		/* Swift is delay-only: an ECN mark carries no information */
	} else if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_NACK) {
		g_ev_nack++;
		q->n_nack++;
		if (g_algo == HPFT_CC_DCQCN)
			hpft_dq_loss(&q->dq, now);
		else
			q->flags |= 2u;      /* ZTR and Swift cut on their next RTT */
	} else if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT) {
		uint32_t s0 = doca_pcc_dev_get_rtt_req_send_timestamp(event);
		uint32_t s1 = doca_pcc_dev_get_timestamp(event);
		uint32_t d = s1 - s0;

		g_ev_rtt++;
		q->rtt_last = d;
		q->rtt_n++;
		q->pr_inflight = 0;
		q->pr_abort = 0;
		q->pr_pending = 1;
		if (q->rtt_min == 0 || d < q->rtt_min)
			q->rtt_min = d;
		if (g_algo == HPFT_CC_SWIFT) {
			hpft_swift_step(q, d, s1);
			want_rtt = 1;        /* one probe in flight per QP */
		} else if (g_algo == HPFT_CC_ZTR) {
			uint32_t rr = q->cc_rate;

			if ((q->flags & 2u) && d >= ZTR_MAX_DELAY) {
				rr = doca_pcc_dev_fxp_mult(ZTR_NACK_DEC_FACTOR, rr);
				q->flags &= ~2u;
			} else if ((q->flags & 1u) || d >= ZTR_MAX_DELAY) {
				rr = doca_pcc_dev_fxp_mult(ZTR_CNP_DEC_FACTOR, rr);
				q->flags &= ~1u;
			} else if (d > ZTR_BASE_RTT) {
				rr = doca_pcc_dev_fxp_mult(ZTR_DEC_FACTOR, rr);
			} else {
				rr += ZTR_AI;
			}
			if (rr > DOCA_PCC_DEV_MAX_RATE)
				rr = DOCA_PCC_DEV_MAX_RATE;
			if (rr < ZTR_MIN_RATE)
				rr = ZTR_MIN_RATE;
			q->cc_rate = rr;
			want_rtt = 1;
		}
	}

	if (g_algo == HPFT_CC_DCQCN)
		hpft_dq_advance(&q->dq, now);

	/* the per-QP epoch: re-assert the set membership once a millisecond */
	if ((uint32_t)(now - q->epoch_ts) >= HPFT_EPOCH_US) {
		q->epoch_ts = now;
		hpft_q_reassert(q, slot);
	}

	/* ---- the rate: the QP's own CC rate, capped by the set's bucket ---- */
	r = hpft_cc_rate(q);
	if (!g_cc_only) {
		int si = (int)q->set - 1;

		if (si >= 0 && si < HPFT_SETS && g_set[si].budget) {
			volatile hpft_set_t *s = &g_set[si];
			uint32_t R, n;

			hpft_set_epoch(s, now, (uint32_t)si);
			R = s->budget;
			n = s->nlive ? s->nlive : (s->nq ? s->nq : 1u);
			if (g_law == HPFT_LAW_TOKEN || g_law == HPFT_LAW_PROP) {
				uint32_t ceil_, dt;

				hpft_pool_refill(s, now);
				/* Pay for the interval this QP has just covered,
				 * at the rate it was pacing over it: summed over
				 * the set that is exactly what it was allowed to
				 * send, and no QP ever reads another QP's state.
				 * Two tighter accountings were tried and both
				 * cost delivery for no real gain: charging the
				 * whole membership once per period from the
				 * epoch sweep (worst flow-set 0.890 of its
				 * share against 0.975), and settling each
				 * member's unpaid gap there (V2 worst flow-set
				 * 0.913 against 0.969). The gaps a QP does not
				 * pay for are the gaps in which it is not on
				 * the wire either. */
				dt = (uint32_t)(now - q->tok_ts);
				if (dt > HPFT_EPOCH_US)
					dt = HPFT_EPOCH_US;
				q->tok_ts = now;
				s->tok -= (int32_t)(((int64_t)q->paced *
						     HPFT_LINE_B_PER_US * dt) >> 20);
				if (g_law == HPFT_LAW_PROP) {
					/* this QP's share of the set's allowance,
					 * in proportion to its own CC rate */
					int64_t bonus = ((int64_t)s->tok << 20) /
							((int64_t)HPFT_EPOCH_US * HPFT_LINE_B_PER_US);
					int64_t allow = (int64_t)s->budget + bonus;
					uint32_t den = s->sum_cc ? s->sum_cc : 1u;
					int64_t p;

					if (allow < 0)
						allow = 0;
					p = ((int64_t)r * allow) / den;
					if (p > (int64_t)s->budget)
						p = s->budget;
					ceil_ = (uint32_t)p;
				} else {
					ceil_ = hpft_pool_ceiling(s);
				}
				if (ceil_ < r)
					r = ceil_;
				goto shaped;
			}
			if (g_law == HPFT_LAW_EQUAL) {
				r = R / n;
			} else if (g_law == HPFT_LAW_CAP) {
				uint32_t share = R / n;

				r = r < share ? r : share;
			}
shaped:
			;
		} else {
			/* the flow set is not known yet: fail into a bounded
			 * rate rather than line rate, so a joining QP cannot
			 * charge a saturated receiver for a whole control loop */
			uint32_t u = g_unknown_rate;

			r = r < u ? r : u;
		}
	}
	if (r < HPFT_MIN_RATE)
		r = HPFT_MIN_RATE;
	if (g_algo == HPFT_CC_SWIFT) {
		cx[3] = q->sw_cwnd ? q->sw_cwnd : 1u;
		cx[4] = q->sw_rtt_s;
		cx[5] = q->sw_last_dec;
		cx[6] = q->flags;
		cx[7] = q->rtt_last;
	}
	hpft_pacc_settle(q, now);
	q->paced = r;
	results->rate = r;
	results->rtt_req = want_rtt;
}

/*
 * Mailbox from the host: policy, knobs and read-back.
 */
doca_pcc_dev_error_t doca_pcc_dev_user_mailbox_handle(void *request,
						      uint32_t request_size,
						      uint32_t max_response_size,
						      void *response,
						      uint32_t *response_size)
{
	(void)max_response_size;
	*response_size = 0;
	if (request_size < 2 * sizeof(uint32_t))
		return DOCA_PCC_DEV_STATUS_FAIL;
	{
		volatile uint32_t *req = (volatile uint32_t *)request;
		uint32_t ft = req[0];
		uint32_t budget = req[1];

		/* {QP -> set id} map: word0 = 0xB48E|n, then n x {key, set},
		 * key = vhca_id << 24 | qpn (or the bare qpn, see hpft_bind_key) */
		if ((ft & 0xffff0000u) == 0xb48e0000u) {
			uint32_t n = ft & 0xffffu;

			if (request_size < (1 + 2 * n) * sizeof(uint32_t))
				return DOCA_PCC_DEV_STATUS_OK;
			for (uint32_t e = 0; e < n; e++)
				hpft_map_put(req[1 + 2 * e], req[2 + 2 * e]);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* flow-set budgets: word0 = 0xB47F|n, then n x {set id, budget} */
		if ((ft & 0xffff0000u) == 0xb47f0000u) {
			uint32_t n = ft & 0xffffu;

			if (request_size < (1 + 2 * n) * sizeof(uint32_t))
				return DOCA_PCC_DEV_STATUS_OK;
			for (uint32_t e = 0; e < n; e++) {
				uint32_t id = req[1 + 2 * e];
				uint32_t bud = req[2 + 2 * e];
				int si = hpft_set_of(id);

				if (si < 0)
					continue;
				if (!bud) {
					/* retire: empty the slot before letting
					 * go of the id, or a thread claiming it
					 * meanwhile inherits this set's members
					 * and pool level */
					g_set[si].budget = 0;
					g_set[si].nq = 0;
					g_set[si].tok = 0;
					g_set[si].id = 0;
				} else {
					g_set[si].budget =
						bud > DOCA_PCC_DEV_MAX_RATE
							? DOCA_PCC_DEV_MAX_RATE : bud;
				}
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xccd <n>: which tenant CC to run */
		if (ft == 0xccdu) {
			g_algo = budget;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xcce <value> <which>: parameters and arms */
		if (ft == 0xcceu && request_size >= 3 * sizeof(uint32_t)) {
			uint32_t which = req[2];

			if (which == 0) g_dq_ai = budget;
			else if (which == 1) g_dq_hai = budget;
			else if (which == 2) g_dq_time_us = budget ? budget : 1u;
			else if (which == 3) g_dq_f = budget;
			else if (which == 8) g_dq_loss_mdf16 = budget > 65536u ? 65536u : budget;
			else if (which == 9) g_dq_loss_gap_us = budget;
			else if (which == 10) g_dq_loss_rt_reset = budget ? 1u : 0u;
			else if (which == 11) g_dq_clamp_mode = budget ? 1u : 0u;
			else if (which == 12) g_cc_only = budget ? 1u : 0u;
			else if (which == 16) { g_dq_g16 = budget > 65535u ? 65535u : budget; hpft_dq_gpow_init(); }
			else if (which == 17) g_dq_alpha_us = budget ? budget : 1u;
			else if (which == 18) g_dq_bytes = budget ? budget : 1u;
			else if (which == 19) g_dq_monitor_us = budget;
			else if (which == 21) g_unknown_rate = budget ? budget : DOCA_PCC_DEV_MAX_RATE;
			else if (which == 22) g_law = budget > 3u ? HPFT_LAW_TOKEN : budget;
			else if (which == 24) g_active_us = budget ? budget : 1000u;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xcd1 <value> <which>: Swift parameters */
		if (ft == 0xcd1u && request_size >= 3 * sizeof(uint32_t)) {
			uint32_t which = req[2];

			if (which == 0) g_sw_base_target = budget;
			else if (which == 1) g_sw_fs_range = budget;
			else if (which == 2) g_sw_alpha_ns = budget;
			else if (which == 3) g_sw_beta_ns = budget;
			else if (which == 4) g_sw_ai = budget ? budget : 1u;
			else if (which == 5) g_sw_beta16 = budget;
			else if (which == 6) g_sw_max_mdf16 = budget;
			else if (which == 7) g_sw_use_srtt = budget ? 1u : 0u;
			else if (which == 8) g_sw_rate_srtt = budget > 3u ? 0u : budget;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xded <slot>: per-flow-set read-back: id, R, rate x time the
		 * set was paced at since this was last read (fxp20 us >> 14 -
		 * NOT a rate: the host divides by the interval between its own
		 * queries), sum of cc over the whole list, sum of cc over
		 * drawing QPs, nlive, nq */
		if (ft == 0xdedu) {
			volatile hpft_set_t *s = &g_set[budget % HPFT_SETS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;
			uint32_t sum_paced = 0, sum_cc = 0, k;

			for (k = 0; k < s->nq && k < HPFT_SET_QPS; k++) {
				uint32_t si = s->qslot[k];

				if (si < HPFT_QSLOTS) {
					sum_paced += g_q[si].pacc;
					g_q[si].pacc = 0;
					sum_cc += hpft_cc_rate(&g_q[si]);
				}
			}
			rsp[0] = s->id;
			rsp[1] = s->budget;
			rsp[2] = sum_paced;
			rsp[3] = sum_cc;
			rsp[4] = s->sum_cc;
			rsp[5] = s->nlive;
			rsp[6] = s->nq;
			rsp[7] = 0;
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xdee <slot>: per-QP read-back (qpn, set, cc rate, the set's
		 * sum_cc, paced, last rtt, cnp count, vhca_id) */
		if (ft == 0xdeeu) {
			volatile hpft_q_t *q = &g_q[budget % HPFT_QSLOTS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = q->qpn;
			rsp[1] = q->set;
			rsp[2] = hpft_cc_rate(q);
			rsp[3] = g_algo == HPFT_CC_SWIFT ? q->sw_cwnd
				 : (q->set ? g_set[(q->set - 1) % HPFT_SETS].sum_cc : 0);
			rsp[4] = q->paced;
			rsp[5] = g_algo == HPFT_CC_SWIFT ? (q->rtt_min << 16 | (q->rtt_last & 0xffffu)) : q->rtt_last;
			rsp[6] = q->n_cnp;
			rsp[7] = q->vhca;
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xdf3: binding diagnostics */
		if (ft == 0xdf3u) {
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = g_q_reinit;
			rsp[1] = g_q_unbind;
			rsp[2] = g_q_next;          /* the spare-slot cursor: it sits at
					     * HPFT_QHOME until keyed placement
					     * has had to give up */
			rsp[3] = g_q_bound;
			rsp[4] = g_q_alloc;
			rsp[5] = g_ev_tx;
			rsp[6] = g_unbind_map;      /* let go because the map disagreed */
			rsp[7] = g_unbind_quiet;    /* let go because the QP went quiet */
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		/* 0xdef: event and binding counters */
		if (ft == 0xdefu) {
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = g_ev_tx;
			rsp[1] = g_ev_cnp;
			rsp[2] = g_ev_nack;
			rsp[3] = g_ev_rtt;
			rsp[4] = g_q_alloc;
			rsp[5] = g_q_bound;
			rsp[6] = g_algo;
			rsp[7] = g_cc_only | (g_law << 8);
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
	}
	return DOCA_PCC_DEV_STATUS_OK;
}

/*
 * Main entry point to user algorithm initialization.
 *
 * @disable_event_bitmask [out]: event types to drop.
 */
void doca_pcc_dev_user_init(uint32_t *disable_event_bitmask)
{
	uint32_t algo_idx = 0, algo_slot = 0, algo_en = 1;

	rtt_template_init(algo_idx);

	for (int port_num = 0; port_num < DOCA_PCC_DEV_MAX_NUM_PORTS; ++port_num) {
		doca_pcc_dev_init_algo_slot(port_num, algo_slot, algo_idx, algo_en);
		doca_pcc_dev_trace_5(0, port_num, algo_idx, algo_slot, algo_en, DOCA_PCC_DEV_EVNT_ROCE_ACK_MASK);
	}

	*disable_event_bitmask = DOCA_PCC_DEV_EVNT_ROCE_ACK_MASK;
	if (DOCA_PCC_DEV_ACK_NACK_TX_EVENT_DISABLED_SUPPORTED == 1)
		*disable_event_bitmask |= (1 << DOCA_PCC_DEV_EVNT_ROCE_TX_FOR_ACK_NACK);

	hpft_dq_gpow_init();
	doca_pcc_dev_printf("%s, disable_event_bitmask=0x%x\n", __func__, *disable_event_bitmask);
	doca_pcc_dev_trace_flush();
}

/*
 * Called when the parameter change was set externally.
 */
doca_pcc_dev_error_t doca_pcc_dev_user_set_algo_params(uint32_t port_num,
						       uint32_t algo_slot,
						       uint32_t param_id_base,
						       uint32_t param_num,
						       const uint32_t *new_param_values,
						       uint32_t *params)
{
	doca_pcc_dev_error_t ret = DOCA_PCC_DEV_STATUS_OK;

	switch (algo_slot) {
	case 0: {
		uint32_t algo_idx = doca_pcc_dev_get_algo_index(port_num, algo_slot);

		if (algo_idx == 0)
			ret = rtt_template_set_algo_params(param_id_base, param_num, new_param_values, params);
		else
			ret = DOCA_PCC_DEV_STATUS_FAIL;
		break;
	}
	default:
		break;
	}
	return ret;
}
