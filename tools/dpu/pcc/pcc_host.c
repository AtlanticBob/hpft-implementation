/*
 * Copyright (c) 2022-2025 NVIDIA CORPORATION AND AFFILIATES.  All rights reserved.
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

#include <stdlib.h>
#include <time.h>
#include <signal.h>
#include <stdbool.h>

#include <doca_argp.h>
#include <doca_dev.h>
#include <doca_log.h>
#include <doca_pcc.h>

#include "pcc_core.h"

static const char *status_str[DOCA_PCC_PS_ERROR + 1] = {"Active", "Standby", "Deactivated", "Error"};
static bool host_stop;
int log_level;
static bool got_debug_sig;

/*
 * Signal sigusr1 handler
 *
 * @signum [in]: signal number
 */
static void siguser1_handler(int signum)
{
	if (signum == SIGUSR1) {
		got_debug_sig = true;
	}
}

/*
 * Signal sigint handler
 *
 * @dummy [in]: Dummy parameter because this handler must accept parameter of type int
 */
static void sigint_handler(int dummy)
{
	(void)dummy;
	host_stop = true;
	signal(SIGINT, SIG_DFL);
}

/*
 * Application main function
 *
 * @argc [in]: command line arguments size
 * @argv [in]: array of command line arguments
 * @return: EXIT_SUCCESS on success and EXIT_FAILURE otherwise
 */
int main(int argc, char **argv)
{
	struct pcc_config cfg = {0};
	struct pcc_resources resources = {0};
	doca_pcc_process_state_t process_status;
	doca_error_t result, tmp_result;
	int exit_status = EXIT_FAILURE;
	bool enable_debug = false;
	struct doca_log_backend *sdk_log;

	/* Set the default configuration values (Example values) */
	cfg.wait_time = -1;
	cfg.role = PCC_ROLE_RP;
	cfg.app = pcc_rp_rtt_template_app;
	memcpy(cfg.threads_list, default_pcc_rp_threads_list, sizeof(default_pcc_rp_threads_list));
	cfg.threads_num = PCC_RP_THREADS_NUM_DEFAULT_VALUE;
	cfg.probe_packet_format = PCC_DEV_PROBE_PACKET_CCMAD;
	cfg.remote_sw_handler = false;
	cfg.hop_limit = IFA2_HOP_LIMIT_DEFAULT_VALUE;
	cfg.gns = IFA2_GNS_DEFAULT_VALUE;
	cfg.gns_ignore_value = IFA2_GNS_IGNORE_DEFAULT_VALUE;
	cfg.gns_ignore_mask = IFA2_GNS_IGNORE_DEFAULT_MASK;
	strcpy(cfg.coredump_file, PCC_COREDUMP_FILE_DEFAULT_PATH);
	log_level = LOG_LEVEL_INFO;

	/* Add SIGINT signal handler for graceful exit */
	if (signal(SIGINT, sigint_handler) == SIG_ERR) {
		PRINT_ERROR("Error: SIGINT error\n");
		return DOCA_ERROR_OPERATING_SYSTEM;
	}
	/* Add SIGUSR1 signal handler for printing debug info */
	if (signal(SIGUSR1, siguser1_handler) == SIG_ERR) {
		PRINT_ERROR("Error: SIGUSR1 error\n");
		return DOCA_ERROR_OPERATING_SYSTEM;
	}

	/* Register a logger backend */
	result = doca_log_backend_create_standard();
	if (result != DOCA_SUCCESS)
		return EXIT_FAILURE;

	/* Register a logger backend for internal SDK errors and warnings */
	result = doca_log_backend_create_with_file_sdk(stderr, &sdk_log);
	if (result != DOCA_SUCCESS)
		return EXIT_FAILURE;
	result = doca_log_backend_set_sdk_level(sdk_log, DOCA_LOG_LEVEL_ERROR);
	if (result != DOCA_SUCCESS)
		return EXIT_FAILURE;

	/* Initialize argparser */
	result = doca_argp_init(NULL, &cfg);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to init ARGP resources: %s\n", doca_error_get_descr(result));
		return EXIT_FAILURE;
	}

	/* Register DOCA PCC application params */
	result = register_pcc_params();
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to register parameters: %s\n", doca_error_get_descr(result));
		goto argp_cleanup;
	}

	/* Start argparser */
	result = doca_argp_start(argc, argv);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to parse input: %s\n", doca_error_get_descr(result));
		goto argp_cleanup;
	}

	/* Get the log level */
	result = doca_argp_get_log_level(&log_level);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to get log level: %s\n", doca_error_get_descr(result));
		goto argp_cleanup;
	}

	/* Initialize DOCA PCC application resources */
	result = pcc_init(&cfg, &resources);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to initialize PCC resources: %s\n", doca_error_get_descr(result));
		goto argp_cleanup;
	}

	PRINT_INFO("Info: Welcome to DOCA Programmable Congestion Control (PCC) application\n");
	PRINT_INFO("Info: Starting DOCA PCC\n");

	/* Start DOCA PCC */
	result = doca_pcc_start(resources.doca_pcc);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to start PCC\n");
		goto destroy_pcc;
	}

	/* Send request to device */
	result = pcc_mailbox_send(&cfg, &resources);
	if (result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to send mailbox request\n");
		goto destroy_pcc;
	}

	host_stop = false;
	PRINT_INFO("Info: Press ctrl + C to exit\n");

	if (getenv("HPFT_RATE_STDIN") != NULL) {
		uint32_t *req_buf = NULL;
		uint32_t mb_response_size, mb_cb_ret_val;
		char line[1400];
		struct timespec t0, t1;

		result = doca_pcc_mailbox_get_request_buffer(resources.doca_pcc, (void **)&req_buf);
		if (result != DOCA_SUCCESS) {
			PRINT_ERROR("Error: HPFT failed to get mailbox request buffer\n");
			goto destroy_pcc;
		}
		PRINT_INFO("Info: HPFT rate-stdin mode ready\n");
		while (!host_stop && fgets(line, sizeof(line), stdin) != NULL) {
			char *cur = line, *tok_end = NULL;
			uint32_t words[128];
			uint32_t nw = 0;

			while (nw < 128) {
				uint32_t v = (uint32_t)strtoul(cur, &tok_end, 0);

				if (tok_end == cur)
					break;
				words[nw++] = v;
				cur = tok_end;
			}
			if (nw < 2)
				continue;
			uint32_t ft = words[0];
			uint32_t rate = words[1];
			if (ft == 0)
				continue;
			clock_gettime(CLOCK_REALTIME, &t0);
			for (uint32_t wi = 0; wi < nw; wi++)
				req_buf[wi] = words[wi];
			result = doca_pcc_mailbox_send(resources.doca_pcc,
						       PCC_MAILBOX_REQUEST_SIZE,
						       &mb_response_size,
						       &mb_cb_ret_val);
			clock_gettime(CLOCK_REALTIME, &t1);
			if (mb_response_size >= 8 * sizeof(uint32_t)) {
				uint32_t *rsp = NULL;

				if (doca_pcc_mailbox_get_response_buffer(resources.doca_pcc, (void **)&rsp) == DOCA_SUCCESS && rsp != NULL)
					printf("HPFT_RSP ft=0x%x bud=%u lvl=%u avg16=%u r=%u s16=%u ep=%u evb32=%u\n",
					       rsp[0], rsp[1], rsp[2], rsp[3], rsp[4], rsp[5], rsp[6], rsp[7]);
			}
			printf("HPFT_SET ft=0x%x rate=%u rc=%d cb=%u t0=%lld.%09ld send_ns=%lld\n",
			       ft, rate, (int)result, mb_cb_ret_val,
			       (long long)t0.tv_sec, t0.tv_nsec,
			       (long long)((t1.tv_sec - t0.tv_sec) * 1000000000LL + (t1.tv_nsec - t0.tv_nsec)));
			fflush(stdout);
		}
		PRINT_INFO("Info: HPFT rate-stdin done\n");
	}

	while (!host_stop) {
		if (got_debug_sig) {
			if (enable_debug == false) {
				enable_debug = true;
				result = doca_pcc_enable_debug(resources.doca_pcc, enable_debug);
				if (result != DOCA_SUCCESS) {
					PRINT_ERROR("Error: failed to enable debug\n");
				}
			} else {
				result = doca_pcc_dump_debug(resources.doca_pcc);
				if (result != DOCA_SUCCESS) {
					PRINT_ERROR("Error: failed to dump debug\n");
				}
			}
			got_debug_sig = 0;
		}
		result = doca_pcc_get_process_state(resources.doca_pcc, &process_status);
		if (result != DOCA_SUCCESS) {
			PRINT_ERROR("Error: Failed to query PCC\n");
			goto destroy_pcc;
		}

		PRINT_INFO("Info: PCC host status %s\n", status_str[process_status]);

		if (process_status == DOCA_PCC_PS_DEACTIVATED || process_status == DOCA_PCC_PS_ERROR)
			break;

		PRINT_INFO("Info: Waiting on DOCA PCC\n");
		result = doca_pcc_wait(resources.doca_pcc, cfg.wait_time);
		if (result != DOCA_SUCCESS) {
			PRINT_ERROR("Error: Failed to wait PCC\n");
			goto destroy_pcc;
		}
	}

	PRINT_INFO("Info: Finished waiting on DOCA PCC\n");

	exit_status = EXIT_SUCCESS;

destroy_pcc:
	tmp_result = pcc_destroy(&resources);
	if (tmp_result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to destroy DOCA PCC application resources: %s\n",
			    doca_error_get_descr(tmp_result));
		DOCA_ERROR_PROPAGATE(result, tmp_result);
	}
argp_cleanup:
	tmp_result = doca_argp_destroy();
	if (tmp_result != DOCA_SUCCESS) {
		PRINT_ERROR("Error: Failed to destroy ARGP: %s\n", doca_error_get_descr(tmp_result));
		DOCA_ERROR_PROPAGATE(result, tmp_result);
	}
	return exit_status;
}
