/*
 * hpft TCP-punt (T3.2 feasibility reference, 2026-07-06).
 *
 * WHAT WORKS (proven): switch mode + fdb_def_rule_en=1. A root basic pipe matches
 * IPv4+TCP (shallow parser_meta) and FWD_PIPEs to a child CPU-RSS pipe -> RSS to
 * Arm SW queue 0 (punt to CPU). Unmatched (RoCE/non-TCP/other VFs) miss -> kernel
 * FDB (OVS), coexisting with OVS/RDMA. Each punted TCP packet is re-injected back
 * to the wire via a CPU doca_eth_txq (rx=tx, no loop, no crash). Bidirectional TCP
 * completes transparently at line rate when return traffic is not mis-punted.
 *
 * THE WALL (definitive, see docs/archive/superseded-designs.md): under fdb_def_rule_en=1
 * (needed for OVS coexistence) ONLY parser_meta type registers match at ANY pipe
 * level. Deep header fields (outer.ip4 src/dst, TCP ports, parser_meta.port_id
 * source vport) are NOT available: outer.ip4 silently matches 0 pkts (root AND
 * child), port_id crashes. => host->net-only / src-vnic / direction discrimination
 * is impossible in coexistence mode; it requires fdb_def_rule_en=0 (full FDB
 * takeover = re-implement transparent L2 forwarding for all VFs). This punt is
 * therefore all-TCP (both directions); direction/pacing await that takeover.
 */
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <doca_log.h>
#include <doca_flow.h>
#include <doca_dev.h>
#include <doca_pe.h>
#include <doca_ctx.h>
#include <doca_mmap.h>
#include <doca_buf.h>
#include <doca_buf_inventory.h>
#include <doca_eth_txq.h>
#include <doca_eth_txq_cpu_data_path.h>
#include <doca_error.h>

#include <flow_common.h>
#include "flow_switch_common.h"
#include "flow_eth_common.h"

DOCA_LOG_REGISTER(FLOW_TCP_PUNT);

#define WAIT_SECS 30
#define NB_ENTRIES 2 /* root parser_meta punt entry + child CPU-RSS entry */
#define TX_SLOTS 8192
#define TX_SLOT_SIZE 2048

static struct doca_flow_pipe_entry *punt_entry; /* root IPv4+TCP punt entry (counted) */
static struct doca_flow_pipe_entry *cpu_entry;  /* child CPU-RSS entry */
static volatile uint64_t g_rx_pkts, g_tx_pkts, g_tx_fail;

/* TX (re-inject to wire) resources */
static struct doca_eth_txq *g_txq;
static struct doca_mmap *g_tx_mmap;
static struct doca_buf_inventory *g_tx_inv;
static uint8_t *g_tx_mem;
static uint32_t g_tx_slot;

static void tx_send_cb(struct doca_eth_txq_task_send *task_send,
		       union doca_data task_user_data,
		       union doca_data ctx_user_data)
{
	struct doca_buf *pkt = NULL;
	(void)task_user_data;
	(void)ctx_user_data;
	if (doca_eth_txq_task_send_get_pkt(task_send, &pkt) == DOCA_SUCCESS && pkt)
		doca_buf_dec_refcount(pkt, NULL);
	if (doca_task_get_status(doca_eth_txq_task_send_as_doca_task(task_send)) == DOCA_SUCCESS)
		g_tx_pkts++;
	else
		g_tx_fail++;
	doca_task_free(doca_eth_txq_task_send_as_doca_task(task_send));
}

/* re-inject each received TCP packet back to the wire */
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
	for (uint16_t i = 0; i < packets_count; i++) {
		void *data = NULL;
		size_t len = 0;
		struct doca_buf *txbuf = NULL;
		struct doca_eth_txq_task_send *task = NULL;
		uint32_t slot;
		uint8_t *dst;

		if (doca_buf_get_data(pkt_array[i], &data) != DOCA_SUCCESS ||
		    doca_buf_get_data_len(pkt_array[i], &len) != DOCA_SUCCESS || len == 0 || len > TX_SLOT_SIZE)
			continue;
		slot = (g_tx_slot++) % TX_SLOTS;
		dst = g_tx_mem + (size_t)slot * TX_SLOT_SIZE;
		memcpy(dst, data, len);
		if (doca_buf_inventory_buf_get_by_data(g_tx_inv, g_tx_mmap, dst, len, &txbuf) != DOCA_SUCCESS) {
			g_tx_fail++;
			continue;
		}
		if (doca_eth_txq_task_send_allocate_init(g_txq, txbuf, (union doca_data){0}, &task) != DOCA_SUCCESS) {
			doca_buf_dec_refcount(txbuf, NULL);
			g_tx_fail++;
			continue;
		}
		if (doca_task_submit(doca_eth_txq_task_send_as_doca_task(task)) != DOCA_SUCCESS) {
			doca_task_free(doca_eth_txq_task_send_as_doca_task(task));
			doca_buf_dec_refcount(txbuf, NULL);
			g_tx_fail++;
		}
	}
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

static doca_error_t setup_tx(struct doca_dev *dev, struct doca_pe *pe)
{
	struct doca_ctx *ctx;
	doca_error_t r;

	g_tx_mem = aligned_alloc(4096, (size_t)TX_SLOTS * TX_SLOT_SIZE);
	if (!g_tx_mem)
		return DOCA_ERROR_NO_MEMORY;
	if ((r = doca_mmap_create(&g_tx_mmap)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_mmap_add_dev(g_tx_mmap, dev)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_mmap_set_memrange(g_tx_mmap, g_tx_mem, (size_t)TX_SLOTS * TX_SLOT_SIZE)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_mmap_set_permissions(g_tx_mmap, DOCA_ACCESS_FLAG_LOCAL_READ_WRITE)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_mmap_start(g_tx_mmap)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_buf_inventory_create(TX_SLOTS, &g_tx_inv)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_buf_inventory_start(g_tx_inv)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_eth_txq_create(dev, TX_SLOTS, &g_txq)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_eth_txq_set_type(g_txq, DOCA_ETH_TXQ_TYPE_REGULAR)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_eth_txq_task_send_set_conf(g_txq, tx_send_cb, tx_send_cb, TX_SLOTS)) != DOCA_SUCCESS)
		return r;
	ctx = doca_eth_txq_as_doca_ctx(g_txq);
	if ((r = doca_pe_connect_ctx(pe, ctx)) != DOCA_SUCCESS)
		return r;
	if ((r = doca_ctx_start(ctx)) != DOCA_SUCCESS)
		return r;
	return DOCA_SUCCESS;
}

static void teardown_tx(void)
{
	if (g_txq) {
		doca_ctx_stop(doca_eth_txq_as_doca_ctx(g_txq));
		doca_eth_txq_destroy(g_txq);
	}
	if (g_tx_inv)
		doca_buf_inventory_destroy(g_tx_inv);
	if (g_tx_mmap)
		doca_mmap_destroy(g_tx_mmap);
	free(g_tx_mem);
}

/*
 * Child (is_root=false) CPU-punt pipe: everything forwarded here is host->net
 * TCP (the root already filtered by src_ip). Wildcard match -> RSS to Arm queue 0.
 * NOTE: outer.ip4 field matching + FWD_RSS works on a NON-root pipe; it silently
 * matches 0 on a root RSS pipe. So the direction (outer.ip4) match lives in the
 * root (FWD_PIPE, proven ok), and RSS-to-CPU lives here in the child.
 */
static doca_error_t create_cpu_rss_pipe(struct doca_flow_port *port, struct doca_flow_pipe **pipe)
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

	result = doca_flow_pipe_cfg_create(&pipe_cfg, port);
	if (result != DOCA_SUCCESS)
		return result;
	result = set_flow_pipe_cfg(pipe_cfg, "CPU_RSS", DOCA_FLOW_PIPE_BASIC, false);
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

	result = doca_flow_pipe_create(pipe_cfg, &fwd, NULL, pipe);
destroy:
	doca_flow_pipe_cfg_destroy(pipe_cfg);
	return result;
}

/*
 * Root direction pipe: match IPv4+TCP with src_ip = host VF (host->net / sender
 * side) and FWD_PIPE to the child CPU-RSS pipe. Miss (no fwd_miss) falls through
 * to the kernel FDB (OVS): return net->host TCP (src=10.1.0.2), non-TCP, RoCE and
 * other VFs all pass transparently. src_ip is declared changeable here and its
 * concrete value is provided per entry (the switch_to_wire idiom).
 */
static doca_error_t create_tcp_punt_pipe(struct doca_flow_port *port,
					 struct doca_flow_pipe *cpu_pipe,
					 struct doca_flow_pipe **pipe)
{
	struct doca_flow_match match;
	struct doca_flow_monitor monitor;
	struct doca_flow_fwd fwd;
	struct doca_flow_pipe_cfg *pipe_cfg;
	doca_error_t result;

	memset(&match, 0, sizeof(match));
	memset(&fwd, 0, sizeof(fwd));
	memset(&monitor, 0, sizeof(monitor));

	monitor.counter_type = DOCA_FLOW_RESOURCE_TYPE_NON_SHARED;
	/* Root: shallow parser_meta match (IPv4+TCP) -> FWD_PIPE to child. This is all
	 * that's available at the is_root attach point under fdb_def_rule_en=1. The
	 * deep outer.ip4 direction match happens in the child (test: does deep matching
	 * work at a non-root pipe here?). */
	match.parser_meta.outer_l3_type = DOCA_FLOW_L3_META_IPV4;
	match.parser_meta.outer_l4_type = DOCA_FLOW_L4_META_TCP;

	result = doca_flow_pipe_cfg_create(&pipe_cfg, port);
	if (result != DOCA_SUCCESS)
		return result;
	result = set_flow_pipe_cfg(pipe_cfg, "TCP_PUNT", DOCA_FLOW_PIPE_BASIC, true);
	if (result != DOCA_SUCCESS)
		goto destroy;
	result = doca_flow_pipe_cfg_set_match(pipe_cfg, &match, NULL);
	if (result != DOCA_SUCCESS)
		goto destroy;
	result = doca_flow_pipe_cfg_set_monitor(pipe_cfg, &monitor);
	if (result != DOCA_SUCCESS)
		goto destroy;

	fwd.type = DOCA_FLOW_FWD_PIPE;
	fwd.next_pipe = cpu_pipe;

	result = doca_flow_pipe_create(pipe_cfg, &fwd, NULL, pipe);
destroy:
	doca_flow_pipe_cfg_destroy(pipe_cfg);
	return result;
}

/* child CPU-RSS entry: all-zero (wildcard) -> follows the pipe's fixed RSS fwd */
static doca_error_t add_cpu_entry(struct doca_flow_pipe *pipe, struct entries_status *status)
{
	struct doca_flow_match match;
	struct doca_flow_actions actions;

	memset(&match, 0, sizeof(match));
	memset(&actions, 0, sizeof(actions));
	return doca_flow_pipe_basic_add_entry(0, pipe, &match, 0, &actions, NULL, NULL, 0, status, &cpu_entry);
}

/* root entry: all-zero within the pipe's fixed parser_meta match -> FWD_PIPE child */
static doca_error_t add_punt_entry(struct doca_flow_pipe *pipe, struct entries_status *status)
{
	struct doca_flow_match match;
	struct doca_flow_actions actions;

	memset(&match, 0, sizeof(match));
	memset(&actions, 0, sizeof(actions));
	return doca_flow_pipe_basic_add_entry(0, pipe, &match, 0, &actions, NULL, NULL, 0, status,
					      &punt_entry);
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
	struct doca_flow_pipe *cpu_pipe;
	struct doca_pe *pe = NULL;
	struct flow_eth_common_rx_cfg eth_cfg = {0};
	struct flow_eth_common_dev_context *dev_ctx = NULL;
	struct doca_dev *dev = ctx->devs_ctx.devs_manager[0].doca_dev;
	doca_error_t result;
	int secs;

	memset(&status, 0, sizeof(status));

	result = doca_pe_create(&pe);
	if (result != DOCA_SUCCESS)
		return result;

	flow_eth_common_set_dev_cfg(nb_queues, false, (union doca_data){0}, rx_success_cb, rx_error_cb, &eth_cfg);
	result = flow_eth_common_create_dev_resources(dev, pe, &eth_cfg, &dev_ctx);
	if (result != DOCA_SUCCESS)
		goto pe_cleanup;

	result = setup_tx(dev, pe);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to set up TX re-inject: %s", doca_error_get_descr(result));
		goto tx_cleanup;
	}

	resource.mode = DOCA_FLOW_RESOURCE_MODE_PORT;
	resource.nr_counters = 2 * NB_ENTRIES;
	resource.nr_rss = 10;
	result = init_doca_flow(nb_queues, "switch,hws,hairpinq_num=4", &resource, nr_shared_resources);
	if (result != DOCA_SUCCESS)
		goto tx_cleanup;

	ARRAY_INIT(actions_mem_size, ACTIONS_MEM_SIZE(NB_ENTRIES));
	result = init_doca_flow_switch_ports(devs_manager, ctx->devs_ctx.nb_devs, ports, nb_ports,
					     actions_mem_size, &resource);
	if (result != DOCA_SUCCESS)
		goto flow_cleanup;

	/* child CPU-RSS pipe first (root's FWD_PIPE references it) */
	result = create_cpu_rss_pipe(ports[0], &cpu_pipe);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to create CPU-RSS pipe: %s", doca_error_get_descr(result));
		goto ports_cleanup;
	}
	result = create_tcp_punt_pipe(ports[0], cpu_pipe, &pipe);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to create TCP punt pipe: %s", doca_error_get_descr(result));
		goto ports_cleanup;
	}
	result = add_cpu_entry(cpu_pipe, &status);
	if (result != DOCA_SUCCESS)
		goto ports_cleanup;
	result = add_punt_entry(pipe, &status);
	if (result != DOCA_SUCCESS)
		goto ports_cleanup;
	result = doca_flow_entries_process(doca_flow_port_switch_get(ports[0]), 0, DEFAULT_TIMEOUT_US, NB_ENTRIES);
	if (result != DOCA_SUCCESS || status.nb_processed != NB_ENTRIES || status.failure) {
		DOCA_LOG_ERR("Failed to process punt entry (processed=%d)", status.nb_processed);
		goto ports_cleanup;
	}

	DOCA_LOG_INFO("TCP-punt+reinject installed (TCP -> Arm CPU -> TX to wire; miss -> kernel). Running %ds...",
		      WAIT_SECS);
	for (secs = 0; secs < WAIT_SECS; secs++) {
		flow_eth_common_handle_pkts(pe, 1);
		if ((secs % 5) == 0)
			DOCA_LOG_INFO("PUNT rx=%lu tx=%lu tx_fail=%lu", (unsigned long)g_rx_pkts,
				      (unsigned long)g_tx_pkts, (unsigned long)g_tx_fail);
	}
	if (doca_flow_resource_query_entry(punt_entry, &query_stats) == DOCA_SUCCESS)
		DOCA_LOG_INFO("PUNT(IPv4+TCP): %lu pkts %lu bytes; reinject rx=%lu tx=%lu fail=%lu",
			      (unsigned long)query_stats.counter.total_pkts,
			      (unsigned long)query_stats.counter.total_bytes,
			      (unsigned long)g_rx_pkts, (unsigned long)g_tx_pkts, (unsigned long)g_tx_fail);

ports_cleanup:
	stop_doca_flow_ports(nb_ports, ports);
flow_cleanup:
	doca_flow_destroy();
tx_cleanup:
	teardown_tx();
	if (dev_ctx)
		flow_eth_common_destroy_dev_resources(dev_ctx);
pe_cleanup:
	if (pe)
		doca_pe_destroy(pe);
	return result;
}
