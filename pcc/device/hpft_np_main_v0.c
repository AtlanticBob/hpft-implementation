/*
 * HPFT NP v0: CCMAD RTT-request responder for receiver-driven caps.
 *
 * Response payload layout (32-bit words, read by the RP via rtt raw data):
 *   w0 = echoed RP send timestamp (bswapped request payload word 0)
 *   w1 = receiver-measured RX rate (2^20 = line-rate units; mailbox-fed)
 *   w2 = receiver-assigned cap    (2^20 = line-rate units; mailbox-fed)
 *
 * v0 serves static globals; the Arm-side agent fills them via mailbox in a
 * later step (per-pair table keyed by the probe packet's IP header comes
 * with P2-2/P2-3d).
 *
 * Derived from the DOCA 2.9 np_nic_telemetry reference (NVIDIA sample code,
 * BSD-3); the 3.4 device API dropped RX-byte NIC counters, so RX measurement
 * moves to the OVS flow-counter agent by design.
 */

#include <doca_pcc_np_dev.h>
#include "pcc_common_dev.h"

volatile uint32_t g_hpft_np_rx_rate = 0x11111111u;
volatile uint32_t g_hpft_np_cap = 0x22222222u;
static volatile uint32_t g_hpft_np_hits;

doca_pcc_dev_error_t doca_pcc_dev_user_mailbox_handle(void *request,
						      uint32_t request_size,
						      uint32_t max_response_size,
						      void *response,
						      uint32_t *response_size)
{
	volatile uint32_t *rsp = (volatile uint32_t *)response;

	(void)request;
	(void)request_size;
	(void)max_response_size;
	rsp[0] = 0x48504654u; /* "HPFT" */
	rsp[1] = g_hpft_np_hits;
	rsp[2] = g_hpft_np_rx_rate;
	rsp[3] = g_hpft_np_cap;
	rsp[4] = 0;
	rsp[5] = 0;
	rsp[6] = 0;
	rsp[7] = 0;
	*response_size = 8 * sizeof(uint32_t);
	return DOCA_PCC_DEV_STATUS_OK;
}

doca_pcc_dev_error_t doca_pcc_dev_np_user_packet_handler(struct doca_pcc_np_dev_request_packet *in,
							 struct doca_pcc_np_dev_response_packet *out)
{
	g_hpft_np_hits++;
	if (out->data != NULL && out->size >= 3 * sizeof(uint32_t)) {
		uint32_t *w = (uint32_t *)(out->data);
		uint32_t ts = 0;

		if (doca_pcc_np_dev_get_payload_size(in) >= sizeof(uint32_t))
			ts = __builtin_bswap32(*((uint32_t *)(doca_pcc_np_dev_get_payload(in))));
		w[0] = ts;
		w[1] = g_hpft_np_rx_rate;
		w[2] = g_hpft_np_cap;
		__dpa_thread_fence(__DPA_MEMORY, __DPA_W, __DPA_W);
	}

	return DOCA_PCC_DEV_STATUS_OK;
}
