/* vport_meter: fresh per-VF class-split byte counters for the rx_agent.
 *
 * Polls QUERY_VPORT_COUNTER (PRM 0x770) over DEVX on the eswitch-manager
 * PF and publishes per-vport {rx_ib, rx_eth, tx_ib, tx_eth} octet/packet
 * counters into a small mmap'd tmpfs file, one seqlock'd 64-byte record
 * per vport, timestamps stamped by this writer (CLOCK_MONOTONIC - the
 * same clock domain as the rx_agent's time.monotonic on this DPU).
 *
 * Why this exists (2026-07-13 measurement redesign): the vport counter's
 * ib/eth split IS the RDMA/kernel-path class split - RoCE bytes land in
 * received_ib_unicast, TCP+ip in received_eth_* - and it is live in FW
 * (no caching quantum; validated byte-exact against the receiver host's
 * port_rcv_data and netdev rx_bytes). Reading it DPU-locally replaces the
 * receiver-host rate exporter, its tmfifo channel and the
 * vport-total-minus-TCP subtraction. The same counters exist in the rep's
 * ethtool -S, but that path is bimodal 120us/3.6ms under OVS (rtnl/state
 * locks); a raw DEVX general command is a steady ~110us per vport, and
 * QUERY_VPORT_COUNTER is on the kernel's uid=0 DEVX whitelist, so no
 * privileges beyond the usual root and no driver changes.
 *
 * usage: vport_meter <ibdev> <mmap_path> <vport,vport,...> [interval_ms]
 *        vports for pf1vfN are N+1 on the pf1 eswitch (host PF = 0).
 *
 * The reader (rx_agent.py VportMeter) treats a record older than 0.1 s as
 * stale and falls back to its old attribution chain, so killing this
 * daemon is always safe (fail-safe, same contract as the rate exporter).
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <infiniband/verbs.h>
#include <infiniband/mlx5dv.h>

#define OPC_QUERY_VPORT_COUNTER 0x770
#define OUTSZ 528
#define MAXV 8

/* out-buffer byte offsets: 16 B header, then 12 traffic counters of
 * {packets u64, octets u64} in PRM order */
enum {
    RECV_IB_UNI = 48, XMIT_IB_UNI = 64,
    RECV_IB_MC = 80, XMIT_IB_MC = 96,
    RECV_ETH_BC = 112, XMIT_ETH_BC = 128,
    RECV_ETH_UNI = 144, XMIT_ETH_UNI = 160,
    RECV_ETH_MC = 176, XMIT_ETH_MC = 192,
};

struct vpm_hdr {                /* 64 B */
    char magic[8];              /* "HPFTVPM1" */
    uint32_t nvports;
    uint32_t interval_us;
    uint16_t vport[MAXV];
    uint8_t pad[64 - 8 - 4 - 4 - 2 * MAXV];
};

struct vpm_rec {                /* 64 B, seqlock (odd seq = writer busy) */
    uint64_t seq;
    uint64_t t_ns;              /* CLOCK_MONOTONIC, midpoint of FW query */
    uint64_t rx_ib;             /* octets to the VF: RoCE */
    uint64_t rx_eth;            /* octets to the VF: kernel path (u+b+m) */
    uint64_t tx_ib;
    uint64_t tx_eth;
    uint64_t rx_ib_pkt;
    uint64_t rx_eth_pkt;
};

static struct ibv_context *ctx;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static uint64_t be64at(const uint8_t *p, int off) {
    uint64_t v;
    memcpy(&v, p + off, 8);
    return __builtin_bswap64(v);
}

static int query(int vport, uint8_t *out) {
    uint8_t in[32];
    memset(in, 0, sizeof in);
    in[0] = OPC_QUERY_VPORT_COUNTER >> 8;
    in[1] = OPC_QUERY_VPORT_COUNTER & 0xff;
    in[8] = 0x80;                               /* other_vport = 1 */
    in[10] = vport >> 8;
    in[11] = vport & 0xff;
    memset(out, 0, OUTSZ);
    int rc = mlx5dv_devx_general_cmd(ctx, in, sizeof in, out, OUTSZ);
    return rc ? rc : out[0];                    /* out[0] = FW status */
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: vport_meter <ibdev> <mmap_path> "
                        "<vport,vport,...> [interval_ms]\n");
        return 2;
    }
    int vports[MAXV], nv = 0;
    for (char *tok = strtok(argv[3], ","); tok && nv < MAXV;
         tok = strtok(NULL, ","))
        vports[nv++] = atoi(tok);
    double interval_ms = argc > 4 ? atof(argv[4]) : 1.0;

    int ndev = 0;
    struct ibv_device **list = ibv_get_device_list(&ndev);
    struct ibv_device *dev = NULL;
    for (int i = 0; i < ndev; i++)
        if (!strcmp(ibv_get_device_name(list[i]), argv[1]))
            dev = list[i];
    if (!dev) { fprintf(stderr, "no such ibdev %s\n", argv[1]); return 2; }
    struct mlx5dv_context_attr attr = { .flags = MLX5DV_CONTEXT_FLAGS_DEVX };
    ctx = mlx5dv_open_device(dev, &attr);
    if (!ctx) { perror("mlx5dv_open_device(DEVX)"); return 2; }

    int fd = open(argv[2], O_RDWR | O_CREAT, 0644);
    if (fd < 0 || ftruncate(fd, 4096)) { perror(argv[2]); return 2; }
    uint8_t *map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED,
                        fd, 0);
    if (map == MAP_FAILED) { perror("mmap"); return 2; }
    memset(map, 0, 4096);
    struct vpm_hdr *hdr = (struct vpm_hdr *)map;
    memcpy(hdr->magic, "HPFTVPM1", 8);
    hdr->nvports = nv;
    hdr->interval_us = (uint32_t)(interval_ms * 1000);
    for (int i = 0; i < nv; i++)
        hdr->vport[i] = vports[i];
    struct vpm_rec *recs = (struct vpm_rec *)(map + 64);

    fprintf(stderr, "vport_meter: %s -> %s, %d vports, %.1fms\n",
            argv[1], argv[2], nv, interval_ms);

    uint8_t out[OUTSZ];
    uint64_t period = (uint64_t)(interval_ms * 1e6);
    uint64_t next = now_ns();
    uint64_t nerr = 0, last_errlog = 0;
    for (;;) {
        for (int i = 0; i < nv; i++) {
            uint64_t t0 = now_ns();
            int rc = query(vports[i], out);
            uint64_t t1 = now_ns();
            if (rc) {
                /* leave the record stale: the reader's 0.1s freshness
                 * check turns persistent failure into a clean fallback */
                nerr++;
                if (t1 - last_errlog > 10000000000ull) {
                    fprintf(stderr, "vport_meter: vport %d rc=%d "
                            "(errs=%llu)\n", vports[i], rc,
                            (unsigned long long)nerr);
                    last_errlog = t1;
                }
                continue;
            }
            struct vpm_rec *r = &recs[i];
            uint64_t s = r->seq;
            __atomic_store_n(&r->seq, s + 1, __ATOMIC_RELAXED);
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            r->t_ns = t0 + (t1 - t0) / 2;
            r->rx_ib = be64at(out, RECV_IB_UNI + 8) +
                       be64at(out, RECV_IB_MC + 8);
            r->rx_eth = be64at(out, RECV_ETH_UNI + 8) +
                        be64at(out, RECV_ETH_BC + 8) +
                        be64at(out, RECV_ETH_MC + 8);
            r->tx_ib = be64at(out, XMIT_IB_UNI + 8) +
                       be64at(out, XMIT_IB_MC + 8);
            r->tx_eth = be64at(out, XMIT_ETH_UNI + 8) +
                        be64at(out, XMIT_ETH_BC + 8) +
                        be64at(out, XMIT_ETH_MC + 8);
            r->rx_ib_pkt = be64at(out, RECV_IB_UNI) +
                           be64at(out, RECV_IB_MC);
            r->rx_eth_pkt = be64at(out, RECV_ETH_UNI) +
                            be64at(out, RECV_ETH_BC) +
                            be64at(out, RECV_ETH_MC);
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            __atomic_store_n(&r->seq, s + 2, __ATOMIC_RELAXED);
        }
        next += period;
        int64_t slack = (int64_t)(next - now_ns());
        if (slack > 0) {
            struct timespec ts = { slack / 1000000000,
                                   slack % 1000000000 };
            nanosleep(&ts, NULL);
        } else {
            next = now_ns();    /* overran: don't try to catch up */
        }
    }
}
