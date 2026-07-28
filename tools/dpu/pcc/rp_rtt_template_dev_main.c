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
#define SAMPLER_THREAD_RANK (0)
#define COUNTERS_SAMPLE_WINDOW_IN_MICROSEC (10)

/**< Counters IDs to configure and read from */
uint32_t counter_ids[DOCA_PCC_DEV_MAX_NUM_PORTS] = {0};
/**< Table of TX bytes counters to sample to */
uint32_t current_sampled_tx_bytes[DOCA_PCC_DEV_MAX_NUM_PORTS] = {0};
/**< Table of TX bytes counters that was last sampled */
uint32_t previous_sampled_tx_bytes[DOCA_PCC_DEV_MAX_NUM_PORTS] = {0};
/**< Last timestamp of sampled counters */
uint32_t last_sample_ts;
/**< Ports active bandwidth. Units of MB/s */
uint32_t ports_bw[DOCA_PCC_DEV_MAX_NUM_PORTS];
/**< Number of available and initiated logical ports */
uint32_t ports_num = 0;
/**< Percentage of the current active ports utilized bandwidth. Saved in FXP 16 format */
uint32_t g_utilized_bw[DOCA_PCC_DEV_MAX_NUM_PORTS];
/**< Flag to indicate that the counters have been initiated */
uint32_t counters_started = 0;

#ifdef DOCA_PCC_SAMPLE_TX_BYTES
/*
 * Dedicate one thread to sample tx bytes counters on a defined time frame,
 * calculate current bandwidth and compare with maximum port bandwidth.
 * This call is enabled by user option to sample TX bytes counter
 */
__attribute__((unused)) FORCE_INLINE void thread0_calc_ports_utilization(void)
{
	uint32_t tx_bytes_delta[DOCA_PCC_DEV_MAX_NUM_PORTS], current_bw[DOCA_PCC_DEV_MAX_NUM_PORTS], ts_delta,
		current_ts;

	if ((doca_pcc_dev_thread_rank() == SAMPLER_THREAD_RANK) && counters_started) {
		current_ts = doca_pcc_dev_get_timer_lo();
		ts_delta = diff_with_wrap32(current_ts, last_sample_ts);
		if (ts_delta >= COUNTERS_SAMPLE_WINDOW_IN_MICROSEC) {
			doca_pcc_dev_nic_counters_sample();
			for (uint32_t i = 0; i < ports_num; i++) {
				tx_bytes_delta[i] =
					diff_with_wrap32(current_sampled_tx_bytes[i], previous_sampled_tx_bytes[i]);
				previous_sampled_tx_bytes[i] = current_sampled_tx_bytes[i];
				current_bw[i] =
					(doca_pcc_dev_fxp_mult(tx_bytes_delta[i], doca_pcc_dev_fxp_recip(ts_delta)) >>
					 16);
				g_utilized_bw[i] = (1 << 16);
				if (current_bw[i] < ports_bw[i])
					g_utilized_bw[i] = doca_pcc_dev_fxp_mult(current_bw[i],
										 doca_pcc_dev_fxp_recip(ports_bw[i]));
			}
			last_sample_ts = current_ts;
		}
	}
}

/**
 * @brief Count the number of available logical ports from queried mask
 *
 * @param[in] ports_mask - ports_mask
 *
 * @return - number of available logical ports initiated in mask
 */
FORCE_INLINE uint32_t count_ports(uint32_t ports_mask)
{
	// find maximum port id enabled. Assume enabled ports are continuous
	return doca_pcc_dev_fls(ports_mask);
}

/**
 * @brief Initiate counter IDs global array on port for TX bytes counter type
 */
FORCE_INLINE void init_counter_ids(void)
{
	for (uint32_t i = 0; i < DOCA_PCC_DEV_MAX_NUM_PORTS; i++)
		counter_ids[i] = DOCA_PCC_DEV_GET_PORT_COUNTER_ID(i, DOCA_PCC_DEV_NIC_COUNTER_TYPE_TX_BYTES, 0);
}

/*
 * Initialize TX counters sampling
 */
FORCE_INLINE void tx_counters_sampling_init(uint32_t portid)
{
	/* number of ports to initiate counters for */
	ports_num = count_ports(doca_pcc_dev_get_logical_ports());
	/* Configure counters to read */
	doca_pcc_dev_nic_counters_config(counter_ids, ports_num, current_sampled_tx_bytes);
	/* save port speed in MBps units */
	ports_bw[portid] = (doca_pcc_dev_mult(doca_pcc_dev_get_port_speed(portid), 1000) >> 3);
	/* Sample counters and save in global table */
	doca_pcc_dev_nic_counters_sample();
	last_sample_ts = doca_pcc_dev_get_timer_lo();
	/* Save sampled TX bytes */
	for (uint32_t i = 0; i < ports_num; i++)
		previous_sampled_tx_bytes[i] = current_sampled_tx_bytes[i];
	counters_started = 1;
}

/*
 * Called on link or port info state change.
 * This callback is used to configure port counters to query TX bytes on
 *
 * @return - void
 */
void doca_pcc_dev_user_port_info_changed(uint32_t portid)
{
	tx_counters_sampling_init(portid);
}
#endif

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
#define HPFT_SETTLE_HOLD (3u)	/* after a cap change, hold the integral ~3 fresh-R steps
				 * (~60ms @50Hz) so the wire responds to the feed-forward
				 * level before the integral reacts to the stale-high R */
#define HPFT_MAX_THREADS (256)

/* byte accumulation is sharded per DPA thread (each thread owns its slot),
 * so no atomics are needed on the hot path; the epoch winner sums the shards */
typedef struct {
	volatile uint32_t flowtag;
	volatile uint32_t dst_tag;	/* dst_ip (0 = any/single-dst) */
	volatile uint32_t budget;
	volatile uint32_t level;
	volatile uint32_t epoch_ts;	/* us, timer_lo domain */
	volatile uint32_t avg_b32_x16;	/* EWMA of 32B-units per packet, x16 fixed point */
	volatile uint32_t port;		/* port this pair was last seen on */
	volatile uint32_t port_cnt_snap;	/* HW port TX-bytes counter snapshot */
	volatile uint32_t port_ev_snap;	/* per-port event-bytes running-total snapshot */
	volatile uint32_t r_ewma;	/* smoothed measured rate, units */
	volatile uint32_t dbg_r_units;
	volatile uint32_t dbg_s_x16;
	volatile uint32_t dbg_epochs;
	volatile uint32_t dbg_ev_b32;
	volatile uint32_t dbg_hits;	/* racy per-event counter for visibility */
	volatile uint32_t remote_rx_rate;	/* receiver-measured RX rate (agent/NP fed) */
	volatile uint32_t last_rrx_used;	/* control-step gating on fresh samples */
	volatile uint32_t hold;		/* settle-hold: skip integral for N fresh-R steps after a cap change */
	volatile uint32_t cc_rate;		/* pair DCQCN-lite term, 2^20 units */
	volatile uint32_t remote_cap;		/* from NP RTT response payload w2 */
	volatile uint32_t qp_count;	/* QPs mapped to this pair (decrease norm) */
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
static volatile uint32_t g_hpft_cnp_any;  /* DIAG: any ROCE_CNP event seen by the callback */
#define HPFT_QPMAP_SIZE (8192)
#define HPFT_QPMAP_PROBE (8)
static volatile uint32_t g_qpn_key[HPFT_QPMAP_SIZE];  /* qpn+1; 0 = empty */
static volatile uint32_t g_qpn_pair[HPFT_QPMAP_SIZE]; /* pair index */
static volatile uint32_t g_qpn_map_active;
static volatile uint32_t g_hpft_rtt_traces;
static volatile uint32_t g_hpft_unknown_ft;
static volatile uint32_t g_hpft_cc_freeze;
static volatile uint32_t g_hpft_ccrate_only;  /* EXPERIMENT 2026-07-22: when set,
	 * results->rate = cc_rate directly (bypass min(cc_rate,level)) -
	 * isolates this PCC-reimplemented DCQCN-style cc_rate state machine's
	 * own convergence behavior from the software budget (level/MIMD),
	 * for a clean "software DCQCN" comparison point against plain
	 * firmware DCQCN (UPCC=0) and the full HPFT system (production
	 * min(cc_rate,level)). Default 0 = production behavior unchanged. */
/* event-observed bytes per port (32B units, running totals, sharded).
 * The HW port counter gives exact TX bytes; the ratio port_true/port_ev is
 * the event-undersampling factor used to correct per-pair estimates. */
static volatile uint32_t g_port_ev[DOCA_PCC_DEV_MAX_NUM_PORTS][HPFT_MAX_THREADS];

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

		if ((ft & 0xffff0000u) == 0xb48d0000u) {
			/* explicit pair config: word0=0xB48D|n, then n x
			 * {pair_idx, flowtag, dst_tag, budget, rx_rate}. budget=0 frees. */
			uint32_t n = ft & 0xffffu;
			volatile uint32_t *req = (volatile uint32_t *)request;

			if (request_size < (1 + 5 * n) * sizeof(uint32_t))
				return DOCA_PCC_DEV_STATUS_OK;
			for (uint32_t e = 0; e < n; e++) {
				uint32_t pidx = req[1 + 5 * e] % HPFT_PAIRS;
				uint32_t eft = req[2 + 5 * e];
				uint32_t edst = req[3 + 5 * e];
				uint32_t ebud = req[4 + 5 * e];
				uint32_t erx = req[5 + 5 * e];
				hpft_pair_t *c = &g_hpft_pairs[pidx];

				if (ebud == 0) {
					c->flowtag = 0;
					continue;
				}
				if (c->flowtag != eft || c->dst_tag != edst || c->budget != ebud) {
					/* proportional feed-forward on a pure cap change (same
					 * pair); a new pair still initialises level=ebud. */
					if (c->flowtag == eft && c->dst_tag == edst && c->budget > 0 && c->level > 0) {
						uint64_t nl = ((uint64_t)c->level * ebud) / c->budget;
						c->level = nl > 0 ? (uint32_t)nl : 1;
						c->last_rrx_used = erx;
						c->hold = HPFT_SETTLE_HOLD;
					} else {
						c->level = ebud;
					}
					c->budget = ebud;
					c->cc_rate = DOCA_PCC_DEV_MAX_RATE;
					if (c->flowtag != eft || c->dst_tag != edst) {
						for (int sh = 0; sh < HPFT_MAX_THREADS; sh++)
							c->b32_shard[sh] = 0;
						c->epoch_ts = 0;
					}
					c->dst_tag = edst;
					c->flowtag = eft;
				}
				c->remote_rx_rate = erx;
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}
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
									if (c->budget > 0 && c->level > 0) {
										uint64_t nl = ((uint64_t)c->level * ebud) / c->budget;
										c->level = nl > 0 ? (uint32_t)nl : 1;
									} else {
										c->level = ebud;
									}
									c->budget = ebud;
									c->last_rrx_used = erx; /* don't integrate on pre-change (stale) R */
									c->hold = HPFT_SETTLE_HOLD;
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
					c->qp_count = 0;	/* relearned from events */
					c->remote_rx_rate = erx;
					for (int s = 0; s < HPFT_MAX_THREADS; s++)
						c->b32_shard[s] = 0;
					c->epoch_ts = 0;
					c->flowtag = eft;
				}
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}

		if (ft == 0xcccu) {
			/* validation: 0xccc <idx> <cc_value>. cc_value>0 freezes cc_rate at
			 * that value (emulates sustained fabric congestion); 0 unfreezes. */
			hpft_pair_t *c = &g_hpft_pairs[((volatile uint32_t *)request)[2] % HPFT_PAIRS];
			uint32_t val = budget;

			if (val > 0) {
				c->cc_rate = val;
				g_hpft_cc_freeze = 1;
			} else {
				g_hpft_cc_freeze = 0;
			}
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xb4a0u) {  /* EXPERIMENT 2026-07-22: 0xb4a0 <0|1> toggles
					 * g_hpft_ccrate_only (rate=cc_rate directly vs
					 * min(cc_rate,level)). Global, all pairs. */
			g_hpft_ccrate_only = budget;
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
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}
		if (ft == 0xdecu) {  /* DIAG 2026-07-22: CNP dispatch/match diagnostic */
			hpft_pair_t *c = &g_hpft_pairs[budget % HPFT_PAIRS];
			volatile uint32_t *rsp = (volatile uint32_t *)response;

			rsp[0] = g_hpft_cnp_any;
			rsp[1] = c->cnp_hits;
			rsp[2] = c->qp_count;
			rsp[3] = c->cc_rate;
			rsp[4] = c->dbg_hits;
			rsp[5] = c->flowtag;
			rsp[6] = 0;
			rsp[7] = 0;
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}

		for (int i = 0; i < HPFT_PAIRS; i++) {
			if (g_hpft_pairs[i].flowtag == ft) {
				if (budget == 0) {
					g_hpft_pairs[i].flowtag = 0;
				} else {
					hpft_pair_t *c = &g_hpft_pairs[i];
					if (c->budget > 0 && c->level > 0) {
						uint64_t nl = ((uint64_t)c->level * budget) / c->budget;
						c->level = nl > 0 ? (uint32_t)nl : 1;
					} else {
						c->level = budget;
					}
					c->budget = budget;
					c->hold = HPFT_SETTLE_HOLD;
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
		if (target >= 0 && qslot >= 0) {
			g_qpn_key[qslot] = qpn + 1;
			g_qpn_pair[qslot] = (uint32_t)target;
			g_hpft_pairs[target].qp_count++;
		}
	}
	if (target >= 0) {
		hpft_pair_t *c = &g_hpft_pairs[target];

		if (a.ev_type == DOCA_PCC_DEV_EVNT_ROCE_CNP) {
			/* fabric congestion: multiplicative decrease (DCQCN-style) */
			c->cnp_hits++;  /* DIAG 2026-07-22 */
			uint32_t nqp = c->qp_count ? c->qp_count : 1;
			uint32_t nr = c->cc_rate - ((c->cc_rate >> 6) / nqp);

			c->cc_rate = (nr < HPFT_MIN_LEVEL) ? HPFT_MIN_LEVEL : nr;
		}
		if (a.ev_type == DOCA_PCC_DEV_EVNT_RTT) {
			uint32_t *w = (uint32_t *)doca_pcc_dev_get_rtt_raw_data(event);

			c->remote_rx_rate = w[1];
			c->remote_cap = w[2];
			if (g_hpft_rtt_traces < 8) {
				g_hpft_rtt_traces++;
				/* format 3: w0(echoed ts), rx_rate, cap, flowtag, now */
				doca_pcc_dev_trace_5(3, w[0], w[1], w[2], ft, now);
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
				g_port_ev[a.port_num & 3][rank] += est;
				c->port = a.port_num & 3;
			}
		}
		c->dbg_hits++;
		uint32_t old = c->epoch_ts;

		if ((uint32_t)(now - old) >= HPFT_EPOCH_US) {
			/* racy claim: concurrent winners are rare and the EWMA absorbs
			 * the occasional double-computed epoch */
			c->epoch_ts = now;
			{
				uint32_t b32 = 0;

				for (int s = 0; s < HPFT_MAX_THREADS; s++) {
					b32 += c->b32_shard[s];
					c->b32_shard[s] = 0;
				}
				uint32_t dt = (uint32_t)(now - old);
				uint32_t ets = old;
				/* correct event undersampling with the exact HW port counter */
				uint32_t prt = c->port;
				uint32_t ev_now = 0;

				for (int s = 0; s < HPFT_MAX_THREADS; s++)
					ev_now += g_port_ev[prt][s];
				uint32_t cnt_now = 0; /* port-counter correction disabled:
							 * counters API faults outside event ctx;
							 * receiver-side RX measurement replaces
							 * this (P2-3 channel) */
				uint32_t true_bytes = cnt_now - c->port_cnt_snap;
				uint32_t ev_d32 = ev_now - c->port_ev_snap;

				c->port_cnt_snap = cnt_now;
				c->port_ev_snap = ev_now;

				uint64_t s_x16 = 16; /* undersampling factor, x16 fixed point */

				if (ev_d32 > 0 && true_bytes > 0) {
					s_x16 = ((uint64_t)true_bytes << 4) / ((uint64_t)ev_d32 * 32u);
					if (s_x16 < 16)
						s_x16 = 16;	/* events can only undercount */
					if (s_x16 > 16 * 64)
						s_x16 = 16 * 64;
				}
				uint64_t b32_corr = ((uint64_t)b32 * s_x16) >> 4;
				/* R in 2^20-of-200G units: bytes32*32B*8b / dt_us / 200e9 * 2^20 */
				uint64_t r_units = (b32_corr << 28) / ((uint64_t)dt * 200000u);
				uint32_t bud = c->budget, lvl = c->level;
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
				c->dbg_s_x16 = (uint32_t)s_x16;
				c->dbg_epochs++;
				c->dbg_ev_b32 = b32;

				if (do_ctrl && c->hold > 0) {
					c->hold--;
					do_ctrl = 0;	/* let the wire catch up to the feed-forward level first */
				}

				if (do_ctrl && ets != 0 && bud > 0) {
					/* small-step integral control: with 1000+ flows the
					 * per-flow rate-application latency (event cadence)
					 * makes fast level swings leave stale-rate mass;
					 * a slowly-moving level converges every flow onto
					 * the same L* and the integral term pins R to B. */
					int64_t err = (int64_t)bud - (int64_t)rs;
					int64_t adj = ((int64_t)lvl * err) / ((int64_t)bud * 8);
					int64_t lim = (int64_t)(lvl >> 3) + 1;

					if (adj == 0 && err != 0)
						adj = (err > 0) ? 1 : -1; /* kill the
									   * truncation deadband */

					if (adj > lim)
						adj = lim;
					if (adj < -lim)
						adj = -lim;
					/* aggregate dig-guard (replaces the per-QP floor
					 * bud>>2). level is PER-QP: results->rate =
					 * min(cc,level) is applied to each of the pair's N
					 * QPs, so the aggregate wire ~= N x level and the
					 * equilibrium level is budget/N. A fixed per-QP floor
					 * is multiplied by N, so bud>>2 let a 1024-QP flow
					 * escape to line rate under a 20G cap (stress L3
					 * 2026-07-12: 1024 QP -> 197G). The collapse the floor
					 * must prevent is the AGGREGATE wire digging to near
					 * zero in apply-lag; guard on the measured aggregate rs
					 * instead: only dig while rs is still above bud>>2.
					 * This floors the AGGREGATE at bud>>2 for any QP count,
					 * so a 1024-QP flow converges to budget/1024 per QP
					 * instead of being pinned at bud>>2 per QP, while a
					 * single flow (aggregate == level) still stops digging
					 * at ~25% of budget as before. */
					if (adj < 0 && rs <= (bud >> 2))
						adj = 0;
					int64_t nl = (int64_t)lvl + adj;

					if (nl < (int64_t)HPFT_MIN_LEVEL)
						nl = HPFT_MIN_LEVEL;
					if (nl > (int64_t)bud)
						nl = bud;
					c->level = (uint32_t)nl;
				}
				/* additive CC recovery toward line rate, every epoch,
				 * independent of the level control step */
				if (!g_hpft_cc_freeze) {
					uint32_t cr = c->cc_rate + (DOCA_PCC_DEV_MAX_RATE >> 8);

					c->cc_rate = (cr < c->cc_rate || cr > DOCA_PCC_DEV_MAX_RATE)
							     ? DOCA_PCC_DEV_MAX_RATE : cr;
				}
				want_rtt = 1;
			}
		}
		uint32_t cc = c->cc_rate;
		uint32_t lvl = c->level;

		results->rate = g_hpft_ccrate_only ? cc : ((cc < lvl) ? cc : lvl);
		results->rtt_req = want_rtt;
		return;
	}
	/* no pair entry for this flowtag: fail open (probe occasionally so the
	 * receiver-driven channel can be tested on any flow) */
	g_hpft_unknown_ft = ft;
	{
		static volatile uint32_t g_hpft_fo_cnt;

		g_hpft_fo_cnt++;
		if ((g_hpft_fo_cnt & 1023u) == 0)
			results->rtt_req = 1;
	}
	results->rate = DOCA_PCC_DEV_MAX_RATE;
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

#ifdef DOCA_PCC_SAMPLE_TX_BYTES
	/** Assuming this is called prior to doca_pcc_dev_user_port_info_changed() */
	init_counter_ids();
#endif

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
