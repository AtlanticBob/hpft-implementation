/*
 * hpft TCP-punt: custom DOCA Flow app (switch mode, fdb_def_rule_en=1).
 * Root pipe matches IPv4+TCP -> RSS to Arm SW queue 0 (punt to CPU).
 * No fwd_miss => unmatched traffic (RoCE, non-TCP, other VFs) falls through to
 * the kernel FDB (OVS) untouched -> RDMA stays on PCC, coexistence preserved.
 * D1: prove flow-level coexistence + selective TCP interception (RX count).
 */
#include <unistd.h>
#include <doca_log.h>
#include <doca_flow.h>
#include <doca_dev.h>
#include <doca_pe.h>
#include <doca_error.h>

#include <flow_common.h>
#include "flow_switch_common.h"
#include "flow_eth_common.h"

DOCA_LOG_REGISTER(FLOW_TCP_PUNT);

#define WAIT_SECS 30
#define NB_ENTRIES 1

static struct doca_flow_pipe_entry *punt_entry;
static volatile uint64_t g_rx_pkts;

static void rx_success_cb(struct doca_eth_rxq_event_batch_managed_recv *event_batch,
			  uint16_t packets_count,
			  union doca_data event_batch_user_data,
			  doca_error_t status,
			  struct doca_buf **pkt_array)
{
	(void)event_batch;
	(void)status;
	(void)event_batch_user_data;
	g_rx_pkts += packets_count;
	DOCA_LOG_INFO("PUNT: received %u TCP packets on Arm queue (total %lu)", packets_count,
		      (unsigned long)g_rx_pkts);
	doca_eth_rxq_event_batch_managed_recv_pkt_array_free(pkt_array);
}

static void rx_error_cb(struct doca_eth_rxq_event_batch_managed_recv *event_batch,
			uint16_t packets_count,
			union doca_data event_batch_user_data,
			doca_error_t status,
			struct doca_buf **pkt_array)
{
	(void)event_batch;
	(void)packets_count;
	(void)event_batch_user_data;
	(void)pkt_array;
	DOCA_LOG_ERR("PUNT rx error: %s", doca_error_get_name(status));
}

/* root pipe: IPv4 + TCP -> RSS queue 0; miss => kernel FDB (no fwd_miss set) */
static doca_error_t create_tcp_punt_pipe(struct doca_flow_port *port, struct doca_flow_pipe **pipe)
{
	struct doca_flow_match match;
	struct doca_flow_monitor monitor;
	struct doca_flow_fwd fwd;
	struct doca_flow_pipe_cfg *pipe_cfg;
	uint16_t rss_queues[1];
	doca_error_t result;

	memset(&match, 0, sizeof(match));
	memset(&fwd, 0, sizeof(fwd));
	memset(&monitor, 0, sizeof(monitor));

	monitor.counter_type = DOCA_FLOW_RESOURCE_TYPE_NON_SHARED;
	match.parser_meta.outer_l3_type = DOCA_FLOW_L3_META_IPV4;
	match.parser_meta.outer_l4_type = DOCA_FLOW_L4_META_TCP;

	result = doca_flow_pipe_cfg_create(&pipe_cfg, port);
	if (result != DOCA_SUCCESS)
		return result;
	result = set_flow_pipe_cfg(pipe_cfg, "TCP_PUNT", DOCA_FLOW_PIPE_BASIC, true /*root*/);
	if (result != DOCA_SUCCESS)
		goto destroy;
	result = doca_flow_pipe_cfg_set_match(pipe_cfg, &match, NULL);
	if (result != DOCA_SUCCESS)
		goto destroy;
	result = doca_flow_pipe_cfg_set_monitor(pipe_cfg, &monitor);
	if (result != DOCA_SUCCESS)
		goto destroy;

	rss_queues[0] = 0;
	fwd.type = DOCA_FLOW_FWD_RSS;
	fwd.rss_type = DOCA_FLOW_RESOURCE_TYPE_NON_SHARED;
	fwd.rss.queues_array = rss_queues;
	fwd.rss.inner_flags = DOCA_FLOW_RSS_IPV4 | DOCA_FLOW_RSS_TCP;
	fwd.rss.nr_queues = 1;

	/* fwd_miss = NULL -> miss falls through to kernel FDB (OVS) */
	result = doca_flow_pipe_create(pipe_cfg, &fwd, NULL, pipe);
destroy:
	doca_flow_pipe_cfg_destroy(pipe_cfg);
	return result;
}

static doca_error_t add_punt_entry(struct doca_flow_pipe *pipe, struct entries_status *status)
{
	struct doca_flow_match match;
	struct doca_flow_actions actions;

	memset(&match, 0, sizeof(match));
	memset(&actions, 0, sizeof(actions));
	return doca_flow_pipe_basic_add_entry(0, pipe, &match, 0, &actions, NULL, NULL, 0, status, &punt_entry);
}

doca_error_t flow_tcp_punt(int nb_queues,
			   int nb_ports,
			   struct flow_devs_manager devs_manager[],
			   struct flow_switch_ctx *ctx)
{
	struct flow_resources resource = {0};
	uint32_t nr_shared_resources[SHARED_RESOURCE_NUM_VALUES] = {0};
	struct doca_flow_port *ports[nb_ports];
	uint32_t actions_mem_size[nb_ports];
	struct doca_flow_resource_query query_stats;
	struct entries_status status;
	struct doca_flow_pipe *pipe;
	struct doca_pe *pe = NULL;
	struct flow_eth_common_rx_cfg eth_cfg = {0};
	struct flow_eth_common_dev_context *dev_ctx = NULL;
	doca_error_t result;

	memset(&status, 0, sizeof(status));

	result = doca_pe_create(&pe);
	if (result != DOCA_SUCCESS)
		return result;

	flow_eth_common_set_dev_cfg(nb_queues, false, (union doca_data){0}, rx_success_cb, rx_error_cb, &eth_cfg);
	result = flow_eth_common_create_dev_resources(ctx->devs_ctx.devs_manager[0].doca_dev, pe, &eth_cfg, &dev_ctx);
	if (result != DOCA_SUCCESS)
		goto pe_cleanup;

	resource.mode = DOCA_FLOW_RESOURCE_MODE_PORT;
	resource.nr_counters = 2 * NB_ENTRIES;
	resource.nr_rss = 10;
	result = init_doca_flow(nb_queues, "switch,hws,hairpinq_num=4", &resource, nr_shared_resources);
	if (result != DOCA_SUCCESS)
		goto dev_cleanup;

	ARRAY_INIT(actions_mem_size, ACTIONS_MEM_SIZE(NB_ENTRIES));
	result = init_doca_flow_switch_ports(devs_manager, ctx->devs_ctx.nb_devs, ports, nb_ports,
					     actions_mem_size, &resource);
	if (result != DOCA_SUCCESS)
		goto flow_cleanup;

	result = create_tcp_punt_pipe(ports[0], &pipe);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to create TCP punt pipe: %s", doca_error_get_descr(result));
		goto ports_cleanup;
	}
	result = add_punt_entry(pipe, &status);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to add punt entry: %s", doca_error_get_descr(result));
		goto ports_cleanup;
	}
	result = doca_flow_entries_process(doca_flow_port_switch_get(ports[0]), 0, DEFAULT_TIMEOUT_US, NB_ENTRIES);
	if (result != DOCA_SUCCESS || status.nb_processed != NB_ENTRIES || status.failure) {
		DOCA_LOG_ERR("Failed to process punt entry (processed=%d)", status.nb_processed);
		goto ports_cleanup;
	}

	DOCA_LOG_INFO("TCP-punt pipe installed (IPv4/TCP -> Arm queue 0; miss -> kernel FDB). Waiting %d s...",
		      WAIT_SECS);
	flow_eth_common_handle_pkts(pe, WAIT_SECS);

	if (doca_flow_resource_query_entry(punt_entry, &query_stats) == DOCA_SUCCESS)
		DOCA_LOG_INFO("PUNT entry counter: %lu pkts, %lu bytes",
			      (unsigned long)query_stats.counter.total_pkts,
			      (unsigned long)query_stats.counter.total_bytes);
	DOCA_LOG_INFO("PUNT: total RX on Arm queue = %lu packets", (unsigned long)g_rx_pkts);

ports_cleanup:
	stop_doca_flow_ports(nb_ports, ports);
flow_cleanup:
	doca_flow_destroy();
dev_cleanup:
	if (dev_ctx)
		flow_eth_common_destroy_dev_resources(&dev_ctx);
pe_cleanup:
	if (pe)
		doca_pe_destroy(pe);
	return result;
}
