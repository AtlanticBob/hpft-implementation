/* hpft TCP-punt main (adapted from flow_switch_rss_main.c) */
#include <stdlib.h>

#include <doca_argp.h>
#include <doca_dev.h>
#include <doca_flow.h>
#include <doca_log.h>
#include <doca_ctx.h>

#include <flow_common.h>
#include <flow_switch_common.h>

DOCA_LOG_REGISTER(FLOW_TCP_PUNT::MAIN);

#define TCP_PUNT_PORTS 2 /* PF/uplink + vf0 representor */

/* keep the default FDB rule so unmatched traffic (RoCE/other) stays on OVS */
#define TCP_PUNT_DEV_ARGS "dv_flow_en=2,fdb_def_rule_en=1,dv_xmeta_en=4"

doca_error_t flow_tcp_punt(int nb_queues,
			   int nb_ports,
			   struct flow_devs_manager devs_manager[],
			   struct flow_switch_ctx *ctx);

int main(int argc, char **argv)
{
	doca_error_t result;
	struct doca_log_backend *sdk_log;
	int exit_status = EXIT_FAILURE;
	const int nb_queues = 10;
	struct flow_switch_ctx ctx = {0};
	uint16_t nr_ports;

	result = doca_log_backend_create_standard();
	if (result != DOCA_SUCCESS)
		goto sample_exit;
	result = doca_log_backend_create_with_file_sdk(stderr, &sdk_log);
	if (result != DOCA_SUCCESS)
		goto sample_exit;
	result = doca_log_backend_set_sdk_level(sdk_log, DOCA_LOG_LEVEL_WARNING);
	if (result != DOCA_SUCCESS)
		goto sample_exit;

	result = doca_argp_init(NULL, &ctx);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to init ARGP: %s", doca_error_get_descr(result));
		goto sample_exit;
	}
	result = register_doca_flow_switch_params();
	if (result != DOCA_SUCCESS)
		goto argp_cleanup;

	ctx.devs_ctx.default_dev_args = TCP_PUNT_DEV_ARGS;

	result = doca_argp_start(argc, argv);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("Failed to parse input: %s", doca_error_get_descr(result));
		goto argp_cleanup;
	}

	nr_ports = ctx.devs_ctx.nb_ports;
	if (nr_ports < TCP_PUNT_PORTS) {
		DOCA_LOG_ERR("Need %d ports, got %d", TCP_PUNT_PORTS, nr_ports);
		goto argp_cleanup;
	}

	result = flow_tcp_punt(nb_queues, TCP_PUNT_PORTS, ctx.devs_ctx.devs_manager, &ctx);
	if (result != DOCA_SUCCESS) {
		DOCA_LOG_ERR("flow_tcp_punt() error: %s", doca_error_get_descr(result));
		goto argp_cleanup;
	}
	exit_status = EXIT_SUCCESS;

argp_cleanup:
	doca_argp_destroy();
sample_exit:
	destroy_doca_flow_devs(&ctx.devs_ctx);
	return exit_status;
}
