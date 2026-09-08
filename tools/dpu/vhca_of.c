/* vhca_of <rdma_dev> <vport>[,<vport>...]
 *
 * Print "<vport> <vhca_id>" for each eswitch vport, asked of the firmware
 * from the DPU, which owns the eswitch (QUERY_HCA_CAP with other_function
 * set, the same route vport_meter.c uses for counters). A host VF cannot
 * answer this about itself: mlx5dv_query_port returns the vhca id only to
 * an eswitch manager. The PCC executor names a QP as (vhca_id, qpn),
 * because QP numbers are unique per function only; the sender agent uses
 * this table to turn the host's (VF, qpn) into the executor's key.
 * Convention on this platform: host pf<P>vf<N> is vport N+1.
 * Build: gcc -O2 -o vhca_of vhca_of.c -libverbs -lmlx5 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <infiniband/verbs.h>
#include <infiniband/mlx5dv.h>

#define OUTSZ (16 + 0x1000)

int main(int argc, char **argv)
{
	int n = 0;
	struct ibv_device **list;
	struct ibv_context *ctx = NULL;
	char *tok;

	if (argc < 3) {
		fprintf(stderr, "usage: %s <rdma_dev> <vport,...>\n", argv[0]);
		return 2;
	}
	list = ibv_get_device_list(&n);
	for (int i = 0; list && i < n; i++)
		if (!strcmp(ibv_get_device_name(list[i]), argv[1]))
			ctx = ibv_open_device(list[i]);
	if (!ctx) {
		fprintf(stderr, "cannot open %s\n", argv[1]);
		return 1;
	}
	for (tok = strtok(argv[2], ","); tok; tok = strtok(NULL, ",")) {
		unsigned vport = (unsigned)strtoul(tok, NULL, 0);
		unsigned char in[16], out[OUTSZ];
		int rc;

		memset(in, 0, sizeof in);
		memset(out, 0, sizeof out);
		in[0] = 0x01; in[1] = 0x00;          /* opcode QUERY_HCA_CAP 0x100 */
		in[6] = 0x00; in[7] = 0x01;          /* op_mod: general caps, current */
		in[8] = 0x80;                        /* other_function = 1 */
		in[10] = (vport >> 8) & 0xff;        /* function_id */
		in[11] = vport & 0xff;
		rc = mlx5dv_devx_general_cmd(ctx, in, sizeof in, out, sizeof out);
		if (rc || out[0]) {
			printf("%u ERR rc=%d status=0x%02x syndrome=0x%02x%02x%02x%02x\n",
			       vport, rc, out[0], out[4], out[5], out[6], out[7]);
			continue;
		}
		/* cmd_hca_cap.vhca_id lives at bits 0x30..0x40 of the capability
		 * block, which begins at byte 16 of the reply */
		printf("%u %u\n", vport, ((unsigned)out[22] << 8) | out[23]);
	}
	ibv_close_device(ctx);
	if (list)
		ibv_free_device_list(list);
	return 0;
}
