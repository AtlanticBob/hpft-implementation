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
#define HPFT_PAIRS (8)
#define HPFT_EPOCH_US (1000u)
#define HPFT_MIN_LEVEL (2u)
#define HPFT_MAX_THREADS (256)

/* byte accumulation is sharded per DPA thread (each thread owns its slot),
 * so no atomics are needed on the hot path; the epoch winner sums the shards */
typedef struct {
	volatile uint32_t flowtag;
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
	volatile uint32_t remote_cap;		/* from NP RTT response payload w2 */
	volatile uint32_t b32_shard[HPFT_MAX_THREADS];
} hpft_pair_t;

static hpft_pair_t g_hpft_pairs[HPFT_PAIRS];
static volatile uint32_t g_hpft_rtt_traces;
static volatile uint32_t g_hpft_unknown_ft;
/* event-observed bytes per port (32B units, running totals, sharded).
 * The HW port counter gives exact TX bytes; the ratio port_true/port_ev is
 * the event-undersampling factor used to correct per-pair estimates. */
static volatile uint32_t g_port_ev[DOCA_PCC_DEV_MAX_NUM_PORTS][HPFT_MAX_THREADS];

static inline uint32_t hpft_cc_rate(void)
{
	/* placeholder for the congestion-control term of rate = min(cc, level);
	 * becomes the DCQCN/RTT-template output after ctx-mapping is resolved */
	return DOCA_PCC_DEV_MAX_RATE;
}

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
						} else {
							if (c->budget != ebud) {
								c->budget = ebud;
								c->level = ebud;
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
					c->remote_rx_rate = erx;
					for (int s = 0; s < HPFT_MAX_THREADS; s++)
						c->b32_shard[s] = 0;
					c->epoch_ts = 0;
					c->flowtag = eft;
				}
			}
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
			rsp[5] = c->dbg_hits;
			rsp[6] = c->dbg_epochs;
			rsp[7] = c->remote_cap;
			*response_size = 8 * sizeof(uint32_t);
			return DOCA_PCC_DEV_STATUS_OK;
		}

		for (int i = 0; i < HPFT_PAIRS; i++) {
			if (g_hpft_pairs[i].flowtag == ft) {
				if (budget == 0) {
					g_hpft_pairs[i].flowtag = 0;
				} else {
					g_hpft_pairs[i].budget = budget;
					g_hpft_pairs[i].level = budget;
				}
				return DOCA_PCC_DEV_STATUS_OK;
			}
			if (free_idx < 0 && g_hpft_pairs[i].flowtag == 0)
				free_idx = i;
		}
		if (budget != 0 && free_idx >= 0) {
			g_hpft_pairs[free_idx].budget = budget;
			g_hpft_pairs[free_idx].level = budget;
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
	for (int i = 0; i < HPFT_PAIRS; i++) {
		hpft_pair_t *c = &g_hpft_pairs[i];

		if (c->flowtag != ft)
			continue;
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
					int64_t nl = (int64_t)lvl + adj;

					int64_t floor_lvl = (int64_t)(bud >> 7) + HPFT_MIN_LEVEL;

					if (nl < floor_lvl)
						nl = floor_lvl; /* keep QPs alive: never crush
								 * below ~0.8% of budget */
					if (nl > (int64_t)bud)
						nl = bud;
					c->level = (uint32_t)nl;
				}
				want_rtt = 1;
			}
		}
		uint32_t cc = hpft_cc_rate();
		uint32_t lvl = c->level;

		results->rate = (cc < lvl) ? cc : lvl;
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
