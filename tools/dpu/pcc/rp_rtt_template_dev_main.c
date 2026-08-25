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
/*
 * Main entry point to user CC algorithm (Reference code)
 * This function starts the algorithm code of a single event
 * It receives the flow context data, the event info and outputs the new rate parameters
 * The function can support multiple algorithms and can call the per algorithm handler based on
 * the algo type. If a single algorithm is required this code can be simplified
 * The function can not be renamed as it is called by the handler infrastructure
 *
 * @algo_ctxt [in]: A pointer to a flow context data retrieved by libpcc.
 * @event [in]: A pointer to an event data structure to be passed to extractor functions
 * @attr [in]: A pointer to additional parameters (algo type).
 * @results [out]: A pointer to result struct to update rate in HW.
 */
/* HPFT v2 (Phase 2 step 1): per-pair shared budget, water-level controller.
 *
 * Pair key: flowtag (source vNIC; dst dimension arrives with receiver-driven
 * caps). Mailbox message {flowtag, budget}: budget in 2^20-of-linerate units,
 * budget=0 deletes. Every flow of a pair is assigned rate = min(cc_rate,
 * level). The level adapts once per epoch (1 ms) from the measured aggregate
 * R of the pair (accumulated from ROCE_TX byte counters):
 *   R > B          -> level *= B/R   (proportional shrink: N greedy flows
 *                                     land on B/N in one step)
 *   R < B - B/16   -> level += level/4 + B/64 (grow into unused budget)
 * This is max-min water-filling without counting flows: O(1) state per pair,
 * works for any QP count. cc_rate is a placeholder (MAX) until DCQCN
 * reintegration. Flows without a pair entry fail open. */
#define HPFT_PAIRS (16)
#define HPFT_EPOCH_US (1000u)
#define HPFT_MIN_LEVEL (2u)

/* ---- reading a CC instead of editing it -------------------------------
 * The executor holds two numbers per flow pair: `level`, the policy share
 * the receiver computed (budget/N), and whatever rate the tenant's
 * congestion control has arrived at. HyperFront's whole involvement with
 * the CC is three steps - read its rate, combine that with the share, and
 * shape the wire to the result. It never writes into the CC, and the CC
 * runs exactly as its author wrote it.
 *
 * That constraint decides the combiner, it does not leave it open.
 *
 *   min(cc, level) - HPFT_COUPLE_MIN, kept as the comparison arm - gives
 *   the CC an unconditional veto over the share, and a CC whose recovery
 *   target is its own pre-congestion rate will use it: every cut moves the
 *   target down with the rate, so a run of cuts walks the pair towards
 *   zero and holds it there for as long as anything keeps the queue
 *   marked. Measured 2026-08-16 on incast8: 4 of 8 runs, 7-22 s with RDMA
 *   at zero while TCP ran at twice its share, and the READ arm lost its
 *   QPs outright to requester timeouts.
 *
 * The lesson is not about DCQCN. A CC's rate is calibrated against the
 * LINK, not against a share the CC has never been told about, and it is
 * free to ratchet. So its absolute value carries nothing the executor can
 * use, and any combiner that reads that value inherits the ratchet. What
 * a CC's rate does carry is its DECREASES: each one is that CC's own
 * judgement, in its own units, that the network asked it to yield.
 *
 * So the executor accumulates the decreases and owns the return:
 *
 *      rate = d * level,   d in [f, 1]
 *      d <- d * (cc / cc_prev)   when the CC lowered its rate
 *      d <- (1 + d) / 2          otherwise, once per recovery period
 *
 * Reading only ratios also means the reading need not be calibrated: any
 * constant factor between "what we sample" and "what the CC would have
 * paced at" cancels. That is what lets the same combiner sit on a
 * DCQCN-style rate in fixed point here and on cwnd/srtt in the TCP
 * shaper.
 *
 * ATTRIBUTION. Letting the CC free-run introduces a loop the edited
 * version did not have: our own shaping makes a queue, the CC reads that
 * queue as congestion and cuts, and if we counted that cut we would shape
 * harder still. The test that breaks it is local - is this flow held back
 * by us, or by the network?
 *
 *   pace-limited (achieved ~= what we programmed): the queue the CC is
 *   reacting to is one we made by holding the flow at its share, and the
 *   share is already the answer to it. The cut is not counted.
 *   below its pace: something other than us is limiting the flow, and
 *   that is exactly the case the executor cannot see or divide. The cut
 *   is counted in full.
 *
 * The rule is self-limiting - as d falls the flow becomes pace-limited
 * again, its cuts stop counting, and d recovers - so the floor f is a
 * backstop rather than the mechanism. It also needs nothing from the
 * receiver: it is per flow and entirely local to the sender.
 *
 * Safety rests where the executor has authority: d <= 1 bounds a flow by
 * its own share, and the shares sum to the root capacity by construction,
 * so no d can put more on that bottleneck than the policy already allows.
 * Where the capacity is not what the allocator thinks, flows cannot reach
 * their pace, stop being pace-limited, and the CC gets its say back.
 */
#define HPFT_D_ONE       (DOCA_PCC_DEV_MAX_RATE)  /* 1.0 in the rates' own fxp20 */
#define HPFT_COUPLE_MIN  (0u)   /* rate = min(cc, level) */
#define HPFT_COUPLE_D    (1u)   /* rate = d * level */
static volatile uint32_t g_hpft_couple = HPFT_COUPLE_D;
static volatile uint32_t g_d_floor = HPFT_D_ONE >> 6;   /* backstop only */
static volatile uint32_t g_d_recover_us = 300u;         /* one half-gap step */
static volatile uint32_t g_pace_pct = 90u;              /* achieved/programmed */

/* ---- congestion-control term selection -------------------------------
 * HyperFront's rate limiting (level = budget/N) is independent of WHICH
 * congestion control runs underneath it: the executor always applies
 * rate = min(cc_rate, level). cc_rate is that underneath-CC. Two are
 * implemented and picked at run time by the host (mailbox 0xccd):
 *   0 = DCQCN-flavoured (CNP -> multiplicative decrease normalised by QP
 *       count, additive recovery per epoch)
 *   1 = ZTR-RTTCC (the vendor's RTT-based algorithm: decrease on NACK /
 *       CNP / rtt above base, additive increase otherwise), ported from
 *       the DOCA rtt_template reference with its shipped parameters.
 * Everything else in the datapath is identical, so a comparison across
 * this switch isolates the CC choice. */
#define HPFT_CC_AIMD  (0u)   /* CNP -> fixed-fraction MD, fixed-step AI */
#define HPFT_CC_DCQCN (2u)   /* the real RP state machine, see below */
#define HPFT_CC_ZTR   (1u)   /* vendor RTT-based algorithm */

/* ---- software DCQCN (RP side) ------------------------------------------
 * Faithful to the published state machine, in rate (not window) form:
 *   on CNP:      Rt = Rc;  Rc = Rc(1 - alpha/2);  alpha += g(1 - alpha)
 *   alpha timer: alpha += g(0 - alpha)          (decay when no CNP)
 *   rate timer / byte counter, both must fire to advance a stage:
 *      stage < F   : fast recovery  Rc = (Rt + Rc)/2
 *      stage >= F  : additive       Rt += AI ; Rc = (Rt + Rc)/2
 *      stage >= 2F : hyper additive Rt += HAI; Rc = (Rt + Rc)/2
 * alpha is fxp16, rates are the device's fxp20 units. The three knobs the
 * firmware exposes as rpg_time_reset / rpg_ai_rate / rpg_hai_rate are the
 * ones below, settable at run time by mailbox 0xcce so the algorithm can
 * be swept the way a firmware DCQCN would be. */
#define DQ_G_FXP16      (1024u)          /* g = 1/64 in fxp16 */
#define DQ_ALPHA_ONE    (65536u)         /* 1.0 in fxp16 */
#define DQ_F_STAGES     (5u)             /* F: fast-recovery steps */
static volatile uint32_t g_dq_ai      = (1u << 20) / 400u;  /* ~0.25% line/step */
static volatile uint32_t g_dq_hai     = (1u << 20) / 80u;   /* ~1.25% line/step */
static volatile uint32_t g_dq_time_us = 300u;               /* rate timer, us */
static volatile uint32_t g_dq_alpha_us = 55u;               /* alpha timer, us */
/* ZTR parameters, values as shipped in rtt_template_algo_params.h */
#define ZTR_UPDATE_FACTOR (((1u << 16) * 10u) / 100u)   /* fxp16 */
#define ZTR_AI            (((1u << 20) * 5u) / 100u)    /* fxp20 rate step */
/* Both thresholds measured on THIS fabric (2026-08-03, device clock, ns):
 * probe rtt_min = 3006, loaded-but-uncongested band 3.3-5.7 us at 26G.
 * BASE_RTT sits above that band so only real queueing triggers decrease;
 * MAX_DELAY = 10x base, the order of the switch's Kmin=400KB queue. */
#define ZTR_BASE_RTT      (7000u)                       /* ns */
#define ZTR_MAX_DELAY     (70000u)                      /* ns */
#define ZTR_MIN_RATE      (1u << (20 - 14))
#define ZTR_DEC_FACTOR      ((1u << 16) - ZTR_UPDATE_FACTOR)
#define ZTR_CNP_DEC_FACTOR  ((1u << 16) - 2u * ZTR_UPDATE_FACTOR)
#define ZTR_NACK_DEC_FACTOR ((1u << 16) - 5u * ZTR_UPDATE_FACTOR)
#define HPFT_MAX_THREADS (256)
#define HPFT_PAIR_QPS (64)          /* QPs tracked per pair for N */
#define HPFT_QP_ACTIVE_EPOCHS (8u)  /* seen within this many epochs = active */
#define HPFT_QP_FORGET_EPOCHS (2000u)

/* byte accumulation is sharded per DPA thread (each thread owns its slot),
 * so no atomics are needed on the hot path; the epoch winner sums the shards */
typedef struct {
	volatile uint32_t flowtag;
	volatile uint32_t dst_tag;	/* dst_ip (0 = any/single-dst) */
	volatile uint32_t budget;
	volatile uint32_t level;
	volatile uint32_t epoch_ts;	/* us, timer_lo domain */
	volatile uint32_t avg_b32_x16;	/* EWMA of 32B-units per packet, x16 fixed point */
	volatile uint32_t r_ewma;	/* smoothed measured rate, units */
	volatile uint32_t dbg_r_units;
	volatile uint32_t dbg_epochs;
	volatile uint32_t dbg_ev_b32;
	volatile uint32_t dbg_hits;	/* racy per-event counter for visibility */
	volatile uint32_t remote_rx_rate;	/* receiver-measured RX rate (agent/NP fed) */
	volatile uint32_t last_rrx_used;	/* control-step gating on fresh samples */
	volatile uint32_t cc_rate;		/* the underneath-CC term, 2^20 units */
	volatile uint32_t ztr_flags;		/* bit0 was_cnp, bit1 was_nack (ZTR) */
	volatile uint32_t rtt_last;		/* last measured RTT, device units */
	volatile uint32_t rtt_min;		/* smallest seen (= this fabric's base) */
	volatile uint32_t rtt_n;		/* RTT measurements taken */
	volatile uint32_t rtt_req_n;		/* RTT requests raised (rtt_req=1) */
	volatile uint32_t dq_target;		/* DCQCN Rt */
	volatile uint32_t dq_alpha;		/* DCQCN alpha, fxp16 */
	volatile uint32_t dq_stage;		/* recovery stage counter */
	volatile uint32_t dq_t_rate;		/* last rate-timer tick, device us */
	volatile uint32_t dq_t_alpha;		/* last alpha-timer tick */
	volatile uint32_t remote_cap;		/* from NP RTT response payload w2 */
	volatile uint32_t d;		/* the observer's deviation from the share,
					 * fxp20; 1<<20 = the share itself */
	volatile uint32_t cc_prev;	/* last cc_rate read, to see its decreases */
	volatile uint32_t d_ts;		/* us, last recovery step */
	volatile uint32_t paced;	/* rate last programmed, for the pace test */
	volatile uint32_t d_cuts;	/* decreases the observer counted; d recovers
					 * in about a millisecond, so sampling d
					 * alone cannot show the branch working */
	volatile uint32_t qp_count;	/* QPs sending in the last epoch */
	volatile uint32_t qp_seen;	/* QPs counted so far in THIS epoch */
	/* map slots of the QPs this pair has seen; N = how many of them were
	 * seen within the last HPFT_QP_ACTIVE_EPOCHS epochs (see the epoch
	 * block) */
	volatile uint32_t qp_slot[HPFT_PAIR_QPS];
	volatile uint32_t qp_nslot;
	volatile uint32_t epoch_id;	/* bumped once per epoch, ages the map */
	volatile uint32_t cnp_hits;	/* DIAG 2026-07-22: CNP events matched to THIS pair
					 * (target>=0 && ev_type==ROCE_CNP), vs g_hpft_cnp_any
					 * which counts CNP events reaching the callback at all -
					 * cc_rate observed pinned at MAX under confirmed real
					 * CNP traffic (dense-sampled, 25 queries/17s, zero
					 * variance); splitting "seen" from "matched" isolates
					 * whether the decrease is skipped at dispatch or at
					 * pair lookup. */
	volatile uint32_t b32_shard[HPFT_MAX_THREADS];
} hpft_pair_t;

static hpft_pair_t g_hpft_pairs[HPFT_PAIRS];

/* Is the executor itself what is holding this flow back? Compares what the
 * flow achieved (receiver-measured where available, the event estimate
 * otherwise) against what we last programmed. Both are in the same units,
 * so this is a ratio test and needs no calibration. */
static inline int hpft_pace_limited(volatile hpft_pair_t *c)
{
	uint32_t paced = c->paced;

	if (paced == 0)
		return 0;
	return (uint64_t)c->dbg_r_units * 100u >= (uint64_t)paced * g_pace_pct;
}

/* Read the CC and fold its decreases into d; recover d towards the share on
 * its own period. The CC is never written to - cc_prev is our copy of what
 * we last saw it at. */
static inline void hpft_observe(volatile hpft_pair_t *c, uint32_t now)
{
	uint32_t cc = c->cc_rate;
	uint32_t prev = c->cc_prev;

	if (cc < prev) {
		if (!hpft_pace_limited(c)) {
			/* d <- d * cc/prev. Normalise first so the divide stays
			 * 32-bit: prev is a rate in fxp20, so one shift is
			 * enough to bring it inside 16 bits. */
			uint32_t sh = (prev > 0xffffu) ? 5u : 0u;
			uint32_t pn = prev >> sh;
			uint32_t cn = cc >> sh;
			uint32_t ratio = pn ? ((cn << 16) / pn) : (1u << 16);
			uint32_t nd = (uint32_t)(((uint64_t)c->d * ratio) >> 16);

			c->d = (nd < g_d_floor) ? g_d_floor : nd;
			c->d_cuts++;
		}
		c->cc_prev = cc;
	} else if (cc > prev) {
		/* the CC's own increase is about its own free-running rate,
		 * which nothing is pacing to; it says nothing about ours */
		c->cc_prev = cc;
	}
	if ((uint32_t)(now - c->d_ts) >= g_d_recover_us) {
		c->d_ts = now;
		if (c->d < HPFT_D_ONE)
			c->d = (HPFT_D_ONE >> 1) + (c->d >> 1);
	}
}
static volatile uint32_t g_hpft_cnp_any;  /* DIAG: any ROCE_CNP event seen by the callback */
#define HPFT_QPMAP_SIZE (8192)
#define HPFT_QPMAP_PROBE (8)
static volatile uint32_t g_qpn_key[HPFT_QPMAP_SIZE];  /* qpn+1; 0 = empty */
static volatile uint32_t g_qpn_pair[HPFT_QPMAP_SIZE]; /* pair index */
/* Epoch in which each mapped QP was last counted. The level is assigned as
 * budget/N, so N has to be the number of QPs actually SENDING, not the
 * number ever seen: perftest recreates QPs, and stale entries made the
 * count read 7 and 5 where four were running, which would under-rate the
 * pair by the same ratio. Counting per epoch ages entries out for free -
 * a QP that stops sending simply stops being counted. */
static volatile uint32_t g_qpn_epoch[HPFT_QPMAP_SIZE];
/* the sighting before the last one: a QP counts as active only if it was
 * seen in two different epochs within the window, which keeps a QP that
 * raises one event every few hundred milliseconds (perftest's control QP
 * does) from taking a full 1/N of the pair's budget for 8 ms each time */
static volatile uint32_t g_qpn_epoch_prev[HPFT_QPMAP_SIZE];
static volatile uint32_t g_qpn_map_active;
static volatile uint32_t g_hpft_rtt_traces;
static volatile uint32_t g_hpft_unknown_ft;
static volatile uint32_t g_hpft_cc_algo = HPFT_CC_DCQCN;
/* Rate for a flow whose flowtag has no pair entry yet. Was MAX_RATE (fail
 * open): a joining RDMA flow-set ran at line rate for the ~100 ms until
 * its first budget landed, which on a saturated receiver charged every
 * incumbent's virtual queue with a burst it did not cause (st_vq_split,
 * 2026-08-25). The sender agent sets this at startup (mailbox 0xccf) to a
 * conservative allowance; without an agent it stays MAX, i.e. the old
 * behaviour. */
static volatile uint32_t g_hpft_unknown_rate = DOCA_PCC_DEV_MAX_RATE;
static volatile uint32_t g_hpft_rtt_events;   /* RTT events seen, any flowtag */
static volatile uint32_t g_hpft_rtt_unmatched; /* ... with no pair match */

doca_pcc_dev_error_t doca_pcc_dev_user_mailbox_handle(void *request,
						      uint32_t request_size,
						      uint32_t max_response_size,
						      void *response,
						      uint32_t *response_size)
{
	(void)max_response_size;
	(void)response;
	*response_size = 0;
	if (request_size >= 2 * sizeof(uint32_t)) {
		uint32_t ft = ((volatile uint32_t *)request)[0];
		uint32_t budget = ((volatile uint32_t *)request)[1];
		int free_idx = -1;

		if ((ft & 0xffff0000u) == 0xb48e0000u) {
			/* qpn map: word0=0xB48E|n, then n x {qpn, pair_idx}. */
			uint32_t n = ft & 0xffffu;
			volatile uint32_t *req = (volatile uint32_t *)request;

			if (request_size < (1 + 2 * n) * sizeof(uint32_t))
				return DOCA_PCC_DEV_STATUS_OK;
			for (uint32_t e = 0; e < n; e++) {
				uint32_t qpn = req[1 + 2 * e];
				uint32_t pidx = req[2 + 2 * e];
				uint32_t h = (qpn * 2654435761u) % HPFT_QPMAP_SIZE;

				for (uint32_t pr = 0; pr < HPFT_QPMAP_PROBE; pr++) {
					uint32_t idx = (h + pr) % HPFT_QPMAP_SIZE;

					if (g_qpn_key[idx] == qpn + 1 || g_qpn_key[idx] == 0) {
						if (g_qpn_key[idx] == 0)
							g_hpft_pairs[pidx % HPFT_PAIRS].qp_count++;
						g_qpn_key[idx] = qpn + 1;
						g_qpn_pair[idx] = pidx % HPFT_PAIRS;
						break;
					}
				}
			}
			g_qpn_map_active = 1;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if ((ft & 0xffff0000u) == 0xb47c0000u) {
			/* batch update: word0 = 0xB47C0000|n, then n x
			 * {flowtag, budget, rx_rate}. budget=0 deletes. */
			uint32_t n = ft & 0xffffu;
			volatile uint32_t *req = (volatile uint32_t *)request;

			if (request_size < (1 + 3 * n) * sizeof(uint32_t))
				return DOCA_PCC_DEV_STATUS_OK;
			for (uint32_t e = 0; e < n; e++) {
				uint32_t eft = req[1 + 3 * e];
				uint32_t ebud = req[2 + 3 * e];
				uint32_t erx = req[3 + 3 * e];
				int fidx = -1;

				for (int i = 0; i < HPFT_PAIRS; i++) {
					hpft_pair_t *c = &g_hpft_pairs[i];

					if (c->flowtag == eft) {
						if (ebud == 0) {
							c->flowtag = 0;
							c->qp_count = 0;
						} else {
							if (c->budget != ebud) {
									/* proportional feed-forward: level tracks bud/N,
									 * so rescaling by the cap ratio lands it on the
									 * new bud/N in one step (any N); the small-step
									 * integral then only fine-tunes. level=ebud would
									 * overshoot to Nx target and force a slow (~430ms)
									 * integral descent. */
									{
						uint32_t n = c->qp_count ? c->qp_count : 1;
						uint32_t w = ebud / n;

						c->level = w < HPFT_MIN_LEVEL
							? HPFT_MIN_LEVEL : w;
					}
									c->budget = ebud;
									c->last_rrx_used = erx; /* don't integrate on pre-change (stale) R */
												}
								c->remote_rx_rate = erx;
						}
						fidx = -2;
						break;
					}
					if (fidx < 0 && c->flowtag == 0)
						fidx = i;
				}
				if (fidx >= 0 && ebud != 0) {
					hpft_pair_t *c = &g_hpft_pairs[fidx];

					c->budget = ebud;
					c->level = ebud;
					c->cc_rate = DOCA_PCC_DEV_MAX_RATE;
					c->d = HPFT_D_ONE;
					c->cc_prev = DOCA_PCC_DEV_MAX_RATE;
					c->paced = 0;
					c->qp_count = 0;	/* relearned from events */
					c->qp_nslot = 0;
					c->remote_rx_rate = erx;
					for (int s = 0; s < HPFT_MAX_THREADS; s++)
						c->b32_shard[s] = 0;
					c->epoch_ts = 0;
					c->flowtag = eft;
				}
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}

		if (ft == 0xccau) {
			/* combiner arm: 0xcca <0|1>. 0 = min(cc, level), 1 = the
			 * observed-decrease coupling. Neither touches the CC; the
			 * switch resets only the observer's own state. */
			g_hpft_couple = budget ? HPFT_COUPLE_D : HPFT_COUPLE_MIN;
			for (int i = 0; i < HPFT_PAIRS; i++) {
				g_hpft_pairs[i].d = HPFT_D_ONE;
				g_hpft_pairs[i].cc_prev = g_hpft_pairs[i].cc_rate;
				g_hpft_pairs[i].paced = 0;
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xdedu) {
			/* observer readback: 0xded <pair>. The host prints a fixed
			 * eleven words, so the slots are reused - in its output
			 * ft=flowtag bud=level lvl=paced avg16=achieved r=d
			 * s16=cc_rate ep=cc_prev evb32=pace_limited. */
			hpft_pair_t *c = &g_hpft_pairs[budget % HPFT_PAIRS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = c->flowtag;
			rsp[1] = c->level;
			rsp[2] = c->paced;
			rsp[3] = c->dbg_r_units;
			rsp[4] = c->d;
			rsp[5] = c->cc_rate;
			rsp[6] = c->cc_prev;
			rsp[7] = (uint32_t)hpft_pace_limited(c);
			rsp[8] = c->dbg_hits;   /* events the DPA processed for this pair */
			rsp[9] = c->d_cuts;
			rsp[10] = 0;
			*response_size = 11 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xccdu) {
			/* CC selection: 0xccd <0|1>. 0 = DCQCN-flavoured term,
			 * 1 = ZTR-RTTCC. Applies to every pair; the HyperFront
			 * level control is untouched either way. Resetting the
			 * per-pair CC state on the switch keeps the first epoch
			 * after it from mixing the two algorithms' histories. */
			g_hpft_cc_algo = budget;   /* 0 AIMD, 1 ZTR, 2 DCQCN */
			for (int i = 0; i < HPFT_PAIRS; i++) {
				g_hpft_pairs[i].cc_rate = DOCA_PCC_DEV_MAX_RATE;
				g_hpft_pairs[i].ztr_flags = 0;
				g_hpft_pairs[i].dq_target = DOCA_PCC_DEV_MAX_RATE;
				g_hpft_pairs[i].dq_alpha = DQ_ALPHA_ONE;
				g_hpft_pairs[i].dq_stage = 0;
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xcceu) {
			/* DCQCN tunables: 0xcce <value> <which>, the three knobs
			 * a firmware DCQCN exposes. which: 0 = AI (rpg_ai_rate),
			 * 1 = HAI (rpg_hai_rate), 2 = rate timer us
			 * (rpg_time_reset). Values in the device's fxp20 rate
			 * units / microseconds. */
			uint32_t which = ((volatile uint32_t *)request)[2];

			if (which == 0) g_dq_ai = budget;
			else if (which == 1) g_dq_hai = budget;
			else if (which == 2) g_dq_time_us = budget ? budget : 1u;
			else if (which == 4) g_d_floor = budget;
			else if (which == 5) g_d_recover_us = budget ? budget : 1u;
			else if (which == 6) g_pace_pct = budget;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xccfu) {
			/* 0xccf <rate units>: cap for flows with no pair entry */
			g_hpft_unknown_rate = budget ? budget : DOCA_PCC_DEV_MAX_RATE;
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xdeau) {
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = g_hpft_unknown_ft;
			for (int i = 1; i < 8; i++)
				rsp[i] = 0;
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xdebu) {
			hpft_pair_t *c = &g_hpft_pairs[budget % HPFT_PAIRS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = c->flowtag;
			rsp[1] = c->budget;
			rsp[2] = c->level;
			rsp[3] = c->remote_rx_rate;
			rsp[4] = c->dbg_r_units;
			rsp[5] = c->cc_rate;
			rsp[6] = c->dbg_epochs;
			rsp[7] = c->remote_cap;
			rsp[8] = c->dq_alpha;
			rsp[9] = c->dq_target;
			rsp[10] = c->dq_stage;
			*response_size = 11 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xdecu) {  /* DIAG 2026-07-22: CNP dispatch/match diagnostic */
			hpft_pair_t *c = &g_hpft_pairs[budget % HPFT_PAIRS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = g_hpft_cnp_any;
			rsp[1] = c->cnp_hits;
			rsp[2] = c->qp_count;
			rsp[3] = c->cc_rate;
			rsp[8] = g_hpft_rtt_events;
			rsp[9] = c->rtt_req_n;
			rsp[10] = c->rtt_n;
			rsp[4] = c->dbg_hits;
			rsp[5] = c->flowtag;
			rsp[6] = c->rtt_last;
			rsp[7] = c->rtt_min;
			*response_size = 11 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}

		for (int i = 0; i < HPFT_PAIRS; i++) {
			if (g_hpft_pairs[i].flowtag == ft) {
				if (budget == 0) {
					g_hpft_pairs[i].flowtag = 0;
				} else {
					hpft_pair_t *c = &g_hpft_pairs[i];
					{
						uint32_t n = c->qp_count ? c->qp_count : 1;
						uint32_t w = budget / n;

						c->level = w < HPFT_MIN_LEVEL
							? HPFT_MIN_LEVEL : w;
					}
					c->budget = budget;
				}
				return DOCA_PCC_DEV_STATUS_OK;
			}
			if (free_idx < 0 && g_hpft_pairs[i].flowtag == 0)
				free_idx = i;
		}
		if (budget != 0 && free_idx >= 0) {
			g_hpft_pairs[free_idx].budget = budget;
			g_hpft_pairs[free_idx].level = budget;
			g_hpft_pairs[free_idx].cc_rate = DOCA_PCC_DEV_MAX_RATE;
			g_hpft_pairs[free_idx].dq_target = DOCA_PCC_DEV_MAX_RATE;
			g_hpft_pairs[free_idx].dq_alpha = DQ_ALPHA_ONE;
			g_hpft_pairs[free_idx].dq_stage = 0;
			g_hpft_pairs[free_idx].avg_b32_x16 = 34 * 16; /* ~1088B pkts */
			for (int s = 0; s < HPFT_MAX_THREADS; s++)
				g_hpft_pairs[free_idx].b32_shard[s] = 0;
			g_hpft_pairs[free_idx].epoch_ts = 0;
			g_hpft_pairs[free_idx].flowtag = ft;
		}
	}
	return DOCA_PCC_DEV_STATUS_OK;
}

void doca_pcc_dev_user_algo(doca_pcc_dev_algo_ctxt_t *algo_ctxt,
			    doca_pcc_dev_event_t *event,
			    const doca_pcc_dev_attr_t *attr,
			    doca_pcc_dev_results_t *results)
{
	uint32_t ft = doca_pcc_dev_get_flowtag(event);
	doca_pcc_dev_event_general_attr_t a = doca_pcc_dev_get_ev_attr(event);
	uint32_t now = doca_pcc_dev_get_timer_lo();

	uint32_t want_rtt = 0;

	if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_CNP)  /* DIAG 2026-07-22 */
		g_hpft_cnp_any++;

	(void)algo_ctxt;
	(void)attr;
	results->rtt_req = 0;
	if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT)
		g_hpft_rtt_events++;
	if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT && g_hpft_rtt_traces < 8) {
		uint32_t *w = (uint32_t *)doca_pcc_dev_get_rtt_raw_data(event);

		g_hpft_rtt_traces++;
		/* format 3: raw w0,w1,w2 + flowtag + now (any pair) */
		doca_pcc_dev_trace_5(3, w[0], w[1], w[2], ft, now);
		doca_pcc_dev_trace_flush();
	}
	int target = -1;
	uint32_t qpn = doca_pcc_dev_get_flow_qpn(event);
	uint32_t qh = (qpn * 2654435761u) % HPFT_QPMAP_SIZE;
	int qslot = -1;

	for (uint32_t pr = 0; pr < HPFT_QPMAP_PROBE; pr++) {
		uint32_t idx = (qh + pr) % HPFT_QPMAP_SIZE;

		if (g_qpn_key[idx] == qpn + 1) {
			target = g_qpn_pair[idx];
			break;
		}
		if (g_qpn_key[idx] == 0) {
			qslot = (int)idx;	/* first free slot on this chain */
			break;
		}
	}
	if (target < 0) {
		for (int i = 0; i < HPFT_PAIRS; i++) {
			if (g_hpft_pairs[i].flowtag == ft) {
				target = i;
				break;
			}
		}
		/* Learn the QP here, on the event path.
		 *
		 * qp_count is what normalises the CNP decrease: the cut is
		 * (cc_rate>>6)/nqp precisely so that the AGGREGATE response of
		 * a pair does not scale with how many QPs it happens to have.
		 * It was only ever incremented by the 0xB48E qpn-map mailbox,
		 * and the sender agent does not send that format - it sends
		 * 0xB47C batches - so nqp was 1 for every pair in production
		 * and a 4-QP flow backed off four times harder than designed.
		 *
		 * Alone that is invisible, because a lone flow sitting at its
		 * cap draws no CNPs. Put two senders on one dst, let them
		 * briefly overshoot, and the resulting CNP burst collapses
		 * cc_rate; since recovery is additive (MAX>>8 per epoch) the
		 * wire then stays near zero for seconds. Measured 2026-07-28
		 * as an 8 s limit cycle with both senders stalling together.
		 *
		 * Learning from the event stream makes the count reflect what
		 * is actually running, independently of which mailbox format
		 * the host uses, and it costs one hash probe on a path that
		 * already computed the hash.
		 */
		if (target >= 0 && qslot >= 0 && qpn > 1) {
			g_qpn_key[qslot] = qpn + 1;
			g_qpn_pair[qslot] = (uint32_t)target;
			/* seen now, countable from the NEXT epoch */
			g_qpn_epoch[qslot] = g_hpft_pairs[target].epoch_id;
			qh = (uint32_t)qslot;	/* count it below */
		}
	}
	/* QP0 and QP1 are reserved by the IB spec for management (SMI and
	 * GSI) and never carry data; the GSI QP in particular exists on
	 * every RoCE device, is owned by ib_core, and stays live for the
	 * lifetime of the port. Counting it toward N hands it a full 1/N of
	 * the pair's allowance which it then never uses - measured
	 * 2026-07-28 as one incast pair reading a stable N=5 against four
	 * data QPs and delivering 4/5 of its budget. This is NOT an artefact
	 * of the traffic generator: the same QP is present in any real
	 * deployment, so the exclusion belongs in the device code.
	 */
	if (target >= 0 && qpn > 1) {
		/* Stamp the QP's map slot with this epoch and make sure the
		 * pair knows the slot. N is computed at the epoch boundary as
		 * the number of slots stamped within the last
		 * HPFT_QP_ACTIVE_EPOCHS epochs. The earlier rule - count a QP
		 * only if it raised an event in this epoch AND the previous
		 * one - undercounted whenever TX events for a QP arrived less
		 * than once per millisecond: N read 3 for a 4-QP pair about
		 * half the time, and level = budget/3 put the pair 33% over
		 * its budget (measured 2026-08-25, executor_step). */
		uint32_t eid = g_hpft_pairs[target].epoch_id;

		for (uint32_t pr = 0; pr < HPFT_QPMAP_PROBE; pr++) {
			uint32_t idx = (qh + pr) % HPFT_QPMAP_SIZE;

			if (g_qpn_key[idx] == qpn + 1) {
				if (g_qpn_epoch[idx] != eid) {
					hpft_pair_t *c = &g_hpft_pairs[target];
					uint32_t k, n = c->qp_nslot;
					int known = 0;

					if (n > HPFT_PAIR_QPS)
						n = HPFT_PAIR_QPS;
					for (k = 0; k < n; k++)
						if (c->qp_slot[k] == idx) {
							known = 1;
							break;
						}
					if (!known && n < HPFT_PAIR_QPS) {
						c->qp_slot[n] = idx;
						c->qp_nslot = n + 1;
					}
					g_qpn_epoch_prev[idx] = g_qpn_epoch[idx];
					g_qpn_epoch[idx] = eid;
				}
				break;
			}
			if (g_qpn_key[idx] == 0)
				break;
		}
	}
	if (target >= 0) {
		hpft_pair_t *c = &g_hpft_pairs[target];

		/* freeze gates the MD side too: 0xccc <MAX> pins cc_rate so
		 * rate == level exactly (pure policy plane, no CC term) */
		if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_CNP) {
			c->cnp_hits++;  /* DIAG 2026-07-22 */
			if (g_hpft_cc_algo == HPFT_CC_ZTR) {
				/* ZTR applies the CNP decrease when the next RTT
				 * measurement lands (reference behaviour): latch it */
				c->ztr_flags |= 1u;
			} else if (g_hpft_cc_algo == HPFT_CC_DCQCN) {
				/* Rt = Rc ; Rc = Rc(1 - alpha/2) ; alpha += g(1-alpha) */
				uint32_t a = c->dq_alpha;
				uint32_t cut = (uint32_t)(((uint64_t)c->cc_rate * a) >> 17);

				c->dq_target = c->cc_rate;
				c->cc_rate = (c->cc_rate > cut + HPFT_MIN_LEVEL)
						     ? (c->cc_rate - cut) : HPFT_MIN_LEVEL;
				a += (uint32_t)(((uint64_t)(DQ_ALPHA_ONE - a) * DQ_G_FXP16) >> 16);
				c->dq_alpha = (a > DQ_ALPHA_ONE) ? DQ_ALPHA_ONE : a;
				c->dq_stage = 0;      /* congestion restarts recovery */
			} else {
				/* fabric congestion: multiplicative decrease (DCQCN-style) */
				uint32_t nqp = c->qp_count ? c->qp_count : 1;
				uint32_t nr = c->cc_rate - ((c->cc_rate >> 6) / nqp);

				c->cc_rate = (nr < HPFT_MIN_LEVEL) ? HPFT_MIN_LEVEL : nr;
			}
		}
		if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_NACK &&
		    g_hpft_cc_algo == HPFT_CC_ZTR)
			c->ztr_flags |= 2u;
		if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT) {
			/* The RTT responder is the remote NIC's HW handler; its
			 * payload is the standard response, NOT NP-authored data.
			 * remote_rx_rate/remote_cap stay agent/mailbox-fed only. */
			{
				/* Measure first, always: ZTR's BASE_RTT has to be
				 * this deployment's base RTT, and that is only
				 * knowable from the device's own clock. */
				uint32_t s0 = doca_pcc_dev_get_rtt_req_send_timestamp(event);
				uint32_t s1 = doca_pcc_dev_get_timestamp(event);
				uint32_t d = s1 - s0;

				c->rtt_last = d;
				c->rtt_n++;
				if (c->rtt_min == 0 || d < c->rtt_min)
					c->rtt_min = d;
			}
			if (g_hpft_cc_algo == HPFT_CC_ZTR) {
				/* ZTR-RTTCC core (DOCA rtt_template reference):
				 * NACK / CNP / rtt-above-base each shrink the rate
				 * by their own factor, an on-time measurement grows
				 * it additively. This is the whole CC term; the
				 * HyperFront level is applied on top by min(). */
				uint32_t rtt = c->rtt_last;
				uint32_t r = c->cc_rate;


				if ((c->ztr_flags & 2u) && rtt >= ZTR_MAX_DELAY) {
					r = doca_pcc_dev_fxp_mult(ZTR_NACK_DEC_FACTOR, r);
					c->ztr_flags &= ~2u;
				} else if ((c->ztr_flags & 1u) || rtt >= ZTR_MAX_DELAY) {
					r = doca_pcc_dev_fxp_mult(ZTR_CNP_DEC_FACTOR, r);
					c->ztr_flags &= ~1u;
				} else if (rtt > ZTR_BASE_RTT) {
					r = doca_pcc_dev_fxp_mult(ZTR_DEC_FACTOR, r);
				} else {
					r += ZTR_AI;
				}
				if (r > DOCA_PCC_DEV_MAX_RATE)
					r = DOCA_PCC_DEV_MAX_RATE;
				if (r < ZTR_MIN_RATE)
					r = ZTR_MIN_RATE;
				c->cc_rate = r;
			}
			if (g_hpft_rtt_traces < 8) {
				g_hpft_rtt_traces++;
				/* format 3: rtt_last, rtt_min, count, flowtag, now */
				doca_pcc_dev_trace_5(3, c->rtt_last, c->rtt_min,
						     c->rtt_n, ft, now);
				doca_pcc_dev_trace_flush();
			}
		}
		if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_TX) {
			doca_pcc_dev_roce_tx_cntrs_t tc = doca_pcc_dev_get_roce_tx_cntrs(event);
			uint32_t rank = doca_pcc_dev_thread_rank() % HPFT_MAX_THREADS;
			/* the 16-bit byte counter clips/wraps at 2MB per coalesced
			 * event; the packet counter clips two orders of magnitude
			 * later. Estimate bytes as pkts x avg-pkt-size, where the
			 * average is learned (EWMA) from unclipped events only -
			 * adapts to any MTU/message mix. */
			uint32_t b32 = (uint32_t)tc.sent_32bytes;
			uint32_t pkts = (uint32_t)tc.sent_pkts;

			if (pkts > 0) {
				uint32_t avg = c->avg_b32_x16;

				if (b32 < 32768 && pkts < 900) {
					uint32_t sample = (b32 << 4) / pkts;

					avg = avg + ((int32_t)(sample - avg) >> 4);
					if (avg < 16)
						avg = 16; /* >= 32B/pkt */
					c->avg_b32_x16 = avg;
				}
				uint32_t est = (pkts * avg) >> 4;

				c->b32_shard[rank] += est;
			}
		}
		c->dbg_hits++;
		uint32_t old = c->epoch_ts;

		if ((uint32_t)(now - old) >= HPFT_EPOCH_US) {
			/* racy claim: concurrent winners are rare and the EWMA absorbs
			 * the occasional double-computed epoch */
			c->epoch_ts = now;
			{
				uint32_t n = c->qp_nslot, k, w = 0, act = 0;
				uint32_t eid = c->epoch_id;

				if (n > HPFT_PAIR_QPS)
					n = HPFT_PAIR_QPS;
				for (k = 0; k < n; k++) {
					uint32_t idx = c->qp_slot[k];
					uint32_t age = eid - g_qpn_epoch[idx];

					if (g_qpn_key[idx] == 0 || age > HPFT_QP_FORGET_EPOCHS)
						continue;      /* gone: drop the slot */
					c->qp_slot[w++] = idx;
					if (age <= HPFT_QP_ACTIVE_EPOCHS &&
					    eid - g_qpn_epoch_prev[idx] <= HPFT_QP_ACTIVE_EPOCHS)
						act++;
				}
				c->qp_nslot = w;
				if (act)
					c->qp_count = act;
			}
			c->epoch_id++;
			{
				uint32_t b32 = 0;

				for (int s = 0; s < HPFT_MAX_THREADS; s++) {
					b32 += c->b32_shard[s];
					c->b32_shard[s] = 0;
				}
				uint32_t dt = (uint32_t)(now - old);
				/* R in 2^20-of-200G units: bytes32*32B*8b / dt_us / 200e9 * 2^20 */
				uint64_t r_units = ((uint64_t)b32 << 28) / ((uint64_t)dt * 200000u);
				uint32_t bud = c->budget;
				uint32_t rrx = c->remote_rx_rate;
				uint32_t rs;

				uint32_t do_ctrl = 1;

				if (rrx != 0) {
					/* receiver-measured RX rate (agent-fed): exact,
					 * no event-loss undercount. One control step per
					 * fresh sample - R updates slower than epochs. */
					rs = rrx;
					c->r_ewma = rrx;
					if (rrx == c->last_rrx_used)
						do_ctrl = 0;
					else
						c->last_rrx_used = rrx;
				} else {
					/* fallback: smoothed TX-event estimate */
					rs = c->r_ewma;
					rs = rs + (uint32_t)(((int64_t)r_units - (int64_t)rs) >> 2);
					c->r_ewma = rs;
				}

				c->dbg_r_units = rs;
				c->dbg_epochs++;
				c->dbg_ev_b32 = b32;

				/* The level assignment below is stateless, so it needs
				 * neither a fresh-measurement gate nor a settle hold -
				 * both existed to protect an integral from acting on a
				 * measurement that had not caught up. do_ctrl and rs
				 * survive only as diagnostics. */
				(void)do_ctrl;
				if (bud > 0) {
					/* small-step integral control: with 1000+ flows the
					 * per-flow rate-application latency (event cadence)
					 * makes fast level swings leave stale-rate mass;
					 * a slowly-moving level converges every flow onto
					 * the same L* and the integral term pins R to B. */
					/* The level is ASSIGNED, not searched for.
					 *
					 * results->rate = min(cc_rate, level) is applied
					 * to each of the pair's QPs, so the aggregate the
					 * pair puts on the wire is N x level and the level
					 * that delivers exactly the budget is budget/N.
					 * Both terms are known here: the budget arrives in
					 * the mailbox, and N is counted from the event
					 * stream each epoch. There is nothing left to
					 * search for.
					 *
					 * This is the same move the response law upstream
					 * makes: when the target is known, computing it
					 * beats converging on it. An integral search needs
					 * a step size, a clip, a floor to stop it digging
					 * and a hold to keep it from integrating against a
					 * measurement that has not caught up yet - four
					 * parameters and four ways to wind up. A large
					 * budget step-down is exactly the input that finds
					 * them: measured 2026-07-28, budget 11.25G with the
					 * wire held at 29.3G for ~2 s, then the level dug
					 * through the target to its minimum and both
					 * senders stalled for ~3 s, cycling every 8 s.
					 * Assignment has no state to wind up, so that whole
					 * class of failure has nowhere to live.
					 *
					 * rs is no longer used to steer the level - it stays
					 * only as the diagnostic the probe reports. What
					 * remains dynamic is cc_rate, which is the response
					 * to actual fabric congestion and belongs on its own
					 * timescale; min() lets whichever is tighter bind.
					 *
					 * A QP that is idle this epoch is not counted, so an
					 * unused share is not silently handed to its
					 * siblings here - the allocator upstream owns that
					 * decision and sees it as r < u.
					 */
					uint32_t n = c->qp_count ? c->qp_count : 1;
					uint32_t want = bud / n;

					if (want < HPFT_MIN_LEVEL)
						want = HPFT_MIN_LEVEL;
					c->level = want;
				}
				if (g_hpft_cc_algo == HPFT_CC_AIMD) {
					/* AIMD backstop: fixed additive step per epoch */
					uint32_t cr = c->cc_rate + (DOCA_PCC_DEV_MAX_RATE >> 8);

					c->cc_rate = (cr < c->cc_rate || cr > DOCA_PCC_DEV_MAX_RATE)
							     ? DOCA_PCC_DEV_MAX_RATE : cr;
				} else if (g_hpft_cc_algo == HPFT_CC_DCQCN) {
					/* alpha decays on its own timer; recovery advances
					 * on the rate timer. The epoch is 1 ms, so both
					 * are counted in epochs against their us periods. */
					uint32_t us = now;   /* device timer, us granularity */

					if (us - c->dq_t_alpha >= g_dq_alpha_us) {
						uint32_t a = c->dq_alpha;

						c->dq_alpha = a - (uint32_t)(((uint64_t)a * DQ_G_FXP16) >> 16);
						c->dq_t_alpha = us;
					}
					if (us - c->dq_t_rate >= g_dq_time_us) {
						uint32_t rt = c->dq_target;
						uint32_t rc = c->cc_rate;

						/* Rt is the pre-congestion rate to climb back
						 * to; it can never sit below the current rate
						 * (an uninitialised or stale Rt would otherwise
						 * halve the rate on every timer tick). */
						if (rt < rc)
							rt = rc;

						c->dq_stage++;
						if (c->dq_stage >= 2u * DQ_F_STAGES)
							rt += g_dq_hai;      /* hyper increase */
						else if (c->dq_stage >= DQ_F_STAGES)
							rt += g_dq_ai;       /* additive increase */
						/* below F: pure fast recovery, Rt unchanged */
						if (rt > DOCA_PCC_DEV_MAX_RATE)
							rt = DOCA_PCC_DEV_MAX_RATE;
						c->dq_target = rt;
						rc = (rt >> 1) + (rc >> 1);   /* Rc = (Rt+Rc)/2 */
						c->cc_rate = (rc > DOCA_PCC_DEV_MAX_RATE)
								     ? DOCA_PCC_DEV_MAX_RATE : rc;
						c->dq_t_rate = us;
					}
				}
				want_rtt = 1;
			}
		}
		uint32_t cc = c->cc_rate;
		uint32_t lvl = c->level;

		if (g_hpft_couple == HPFT_COUPLE_D) {
			/* reading the CC is work only this arm needs; the min()
			 * arm must not pay for it, or it stops being the
			 * baseline its measurements are taken as */
			uint32_t r;

			hpft_observe(c, now);
			r = (uint32_t)(((uint64_t)c->d * lvl) >> 20);
			if (r < HPFT_MIN_LEVEL)
				r = HPFT_MIN_LEVEL;
			c->paced = r;
			results->rate = r;
		} else {
			results->rate = (cc < lvl) ? cc : lvl;
		}
		results->rtt_req = want_rtt;
		if (want_rtt)
			c->rtt_req_n++;
		return;
	}
	/* no pair entry for this flowtag: fail open (probe occasionally so the
	 * receiver-driven channel can be tested on any flow) */
	g_hpft_unknown_ft = ft;
	if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT)
		g_hpft_rtt_unmatched++;
	{
		static volatile uint32_t g_hpft_fo_cnt;

		g_hpft_fo_cnt++;
		if ((g_hpft_fo_cnt & 1023u) == 0)
			results->rtt_req = 1;
	}
	results->rate = g_hpft_unknown_rate;
}

/*
 * Main entry point to user algorithm initialization (reference code)
 * This function starts the user algorithm initialization code
 * The function will be called once per process load and should init all supported
 * algorithms and all ports
 *
 * @disable_event_bitmask [out]: user code can tell the infrastructure which event
 * types to ignore (mask out). Events of this type will be dropped and not passed to
 * any algo
 */
void doca_pcc_dev_user_init(uint32_t *disable_event_bitmask)
{
	uint32_t algo_idx = 0, algo_slot = 0, algo_en = 1;

	/* Initialize algorithm with algo_idx=0 */
	rtt_template_init(algo_idx);

	for (int port_num = 0; port_num < DOCA_PCC_DEV_MAX_NUM_PORTS; ++port_num) {
		/* Slot 0 will use algo_idx 0, default enabled */
		doca_pcc_dev_init_algo_slot(port_num, algo_slot, algo_idx, algo_en);
		doca_pcc_dev_trace_5(0, port_num, algo_idx, algo_slot, algo_en, DOCA_PCC_DEV_EVNT_ROCE_ACK_MASK);
	}


	/* disable events of below type */
	*disable_event_bitmask = DOCA_PCC_DEV_EVNT_ROCE_ACK_MASK;
	if (DOCA_PCC_DEV_ACK_NACK_TX_EVENT_DISABLED_SUPPORTED == 1) {
		*disable_event_bitmask |= (1 << DOCA_PCC_DEV_EVNT_ROCE_TX_FOR_ACK_NACK);
	}

	doca_pcc_dev_printf("%s, disable_event_bitmask=0x%x\n", __func__, *disable_event_bitmask);
	doca_pcc_dev_trace_flush();
}

/*
 * Called when the parameter change was set externally.
 * The implementation should:
 *     Check the given new_parameters values. If those are correct from the algorithm perspective,
 *     assign them to the given parameter array.

 * @port_num [in]: index of the port
 * @algo_slot [in]: Algo slot identifier as referred to in the PPCC command field "algo_slot"
 * if possible it should be equal to the algo_idx
 * @param_id_base [in]: id of the first parameter that was changed.
 * @param_num [in]: number of all parameters that were changed
 * @new_param_values [in]: pointer to an array which holds param_num number of new values for parameters
 * @params [in]: pointer to an array which holds beginning of the current parameters to be changed
 * @return -
 * DOCA_PCC_DEV_STATUS_OK: Parameters were set
 * DOCA_PCC_DEV_STATUS_FAIL: the values (one or more) are not legal. No parameters were changed
 */
doca_pcc_dev_error_t doca_pcc_dev_user_set_algo_params(uint32_t port_num,
						       uint32_t algo_slot,
						       uint32_t param_id_base,
						       uint32_t param_num,
						       const uint32_t *new_param_values,
						       uint32_t *params)
{
	/* Notify the user that a change happened to take action.
	 * I.E.: Pre calculate values to be used in the algo that are based on the parameter value.
	 * Support more complex checks. E.G.: Param is a bit mask - min and max do not help
	 * Param dependency checking.
	 */
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
