/*
 * kvfetch -- application-shaped RDMA WRITE transfers for the 2-8 experiments.
 *
 * A prefill or KV-pool node pushing per-request KV-cache blobs to a decode
 * instance (Mooncake-style transfer engine), or a trainer pushing its half of
 * the weights to a rollout instance. Every transfer is cut into fixed slices
 * (default 128 KB) striped round-robin over Q RC QPs, each QP with a bounded
 * number of slices in flight (default 16). Two ways to drive it:
 *
 *   open loop (-S schedule):  requests arrive at the times the schedule says,
 *       whether or not earlier ones are done. At most M requests are in
 *       service at once (default 8); the rest wait in arrival order. The
 *       requests in service share the QPs slice by slice, round-robin. Every
 *       request is logged with its arrival, service start and completion, so
 *       queueing time and transfer time come out separately.
 *   iterations (-I steps:bytes:compute_s):  one transfer per step, then a
 *       compute pause, then the next step. With -G the members of a group
 *       wait for each other at the end of every step (a training step needs
 *       all shards of the weights), so the step's sync time is the slowest
 *       member's.
 *
 *   server (receiving side, passive; one registered region per client):
 *     kvfetch -s -d mlx5_6 -p 19100 [-x 3] [-b 2]
 *   client:
 *     kvfetch -c <server host> -d mlx5_6 -p 19100 [-x 3] [-q 8] [-D 16] [-z 131072]
 *             (-S sched.txt [-M 8] | -I 5:7000000000:10 [-G L:<port>:<n> | -G F:<host>:<port>])
 *             [-A start_epoch] [-t seconds] [-C tclass] [-o out.csv] [-i id] [-m mtu]
 *
 * A schedule line is "<arrival s from start> <bytes> [tokens]". -A is the
 * absolute wall-clock start (the runner's T0 plus the row's offset); the
 * client connects first and then waits for it. -t bounds the run: after it
 * nothing new starts, in-flight slices drain, and every request that did not
 * finish is still logged (status unfinished, or queued if it never started).
 * Times in the CSV are wall-clock seconds.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>
#include <signal.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <infiniband/verbs.h>

#define MAX_QP 64
#define MAX_MEMBERS 16

static const char *dev_name = NULL;
static int port = 19100, gid_idx = 3, ib_port = 1, want_mtu = 0;
static int nqp = 8, depth = 16, tclass = 0, max_active = 8;
static size_t slice = 131072;
static double duration = 60, start_epoch = 0;
static double region_gb = 2.0;
static const char *out_path = NULL, *client_id = "c", *sched_path = NULL, *iter_spec = NULL, *group_spec = NULL;
static volatile sig_atomic_t stop_flag = 0;

struct qp_info { uint32_t qpn, psn; };
struct conn_info {
    uint8_t  gid[16];
    uint32_t rkey;
    uint64_t addr;
    uint64_t len;
    uint32_t nqp;
    uint32_t mtu;
    struct qp_info qp[MAX_QP];
} __attribute__((packed));

static void die(const char *m) { perror(m); exit(1); }
static double now_mono(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static double now_wall(void) { struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static void on_sig(int s) { (void)s; stop_flag = 1; }

static int sock_rw(int fd, void *buf, size_t n, int wr) {
    char *p = buf; size_t done = 0;
    while (done < n) {
        ssize_t r = wr ? send(fd, p + done, n - done, MSG_NOSIGNAL) : recv(fd, p + done, n - done, 0);
        if (r <= 0) return -1;
        done += r;
    }
    return 0;
}

struct rdma_ctx {
    struct ibv_context *ctx;
    struct ibv_pd *pd;
    struct ibv_cq *cq;
    struct ibv_mr *mr;
    void *buf; size_t buflen;
    struct ibv_qp *qp[MAX_QP];
    struct ibv_port_attr pattr;
    union ibv_gid gid;
    int n;
};

static struct ibv_context *open_dev(const char *name) {
    int n; struct ibv_device **l = ibv_get_device_list(&n);
    if (!l) die("ibv_get_device_list");
    for (int i = 0; i < n; i++)
        if (!strcmp(ibv_get_device_name(l[i]), name)) {
            struct ibv_context *c = ibv_open_device(l[i]);
            ibv_free_device_list(l);
            if (!c) die("ibv_open_device");
            return c;
        }
    fprintf(stderr, "device %s not found\n", name); exit(1);
}

static void ctx_init(struct rdma_ctx *r, size_t buflen, int n, int cq_depth, int sq_depth) {
    memset(r, 0, sizeof(*r));
    r->ctx = open_dev(dev_name);
    r->pd = ibv_alloc_pd(r->ctx);
    if (!r->pd) die("ibv_alloc_pd");
    if (ibv_query_port(r->ctx, ib_port, &r->pattr)) die("ibv_query_port");
    if (ibv_query_gid(r->ctx, ib_port, gid_idx, &r->gid)) die("ibv_query_gid");
    r->buflen = buflen;
    r->buf = mmap(NULL, buflen, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    if (r->buf == MAP_FAILED) die("mmap");
    madvise(r->buf, buflen, MADV_HUGEPAGE);
    r->mr = ibv_reg_mr(r->pd, r->buf, buflen, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
    if (!r->mr) die("ibv_reg_mr");
    r->cq = ibv_create_cq(r->ctx, cq_depth, NULL, NULL, 0);
    if (!r->cq) die("ibv_create_cq");
    r->n = n;
    for (int i = 0; i < n; i++) {
        struct ibv_qp_init_attr a = {0};
        a.send_cq = r->cq; a.recv_cq = r->cq; a.qp_type = IBV_QPT_RC;
        a.cap.max_send_wr = sq_depth; a.cap.max_recv_wr = 1;
        a.cap.max_send_sge = 1; a.cap.max_recv_sge = 1;
        r->qp[i] = ibv_create_qp(r->pd, &a);
        if (!r->qp[i]) die("ibv_create_qp");
        struct ibv_qp_attr m = {0};
        m.qp_state = IBV_QPS_INIT; m.pkey_index = 0; m.port_num = ib_port;
        m.qp_access_flags = IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_LOCAL_WRITE;
        if (ibv_modify_qp(r->qp[i], &m, IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS)) die("modify INIT");
    }
}

static void fill_info(struct rdma_ctx *r, struct conn_info *ci) {
    memset(ci, 0, sizeof(*ci));
    memcpy(ci->gid, r->gid.raw, 16);
    ci->rkey = r->mr->rkey; ci->addr = (uint64_t)(uintptr_t)r->buf; ci->len = r->buflen;
    ci->nqp = r->n;
    /* the path MTU the run asks for (-m 4096), or the port's active one */
    ci->mtu = want_mtu == 4096 ? IBV_MTU_4096 : want_mtu == 2048 ? IBV_MTU_2048 :
              want_mtu == 1024 ? IBV_MTU_1024 : r->pattr.active_mtu;
    for (int i = 0; i < r->n; i++) { ci->qp[i].qpn = r->qp[i]->qp_num; ci->qp[i].psn = (rand() & 0xffffff); }
}

static void connect_qps(struct rdma_ctx *r, struct conn_info *mine, struct conn_info *peer) {
    enum ibv_mtu mtu = mine->mtu < peer->mtu ? mine->mtu : peer->mtu;
    for (int i = 0; i < r->n; i++) {
        struct ibv_qp_attr a = {0};
        a.qp_state = IBV_QPS_RTR; a.path_mtu = mtu; a.dest_qp_num = peer->qp[i].qpn;
        a.rq_psn = peer->qp[i].psn; a.max_dest_rd_atomic = 1; a.min_rnr_timer = 12;
        a.ah_attr.is_global = 1; a.ah_attr.dlid = 0; a.ah_attr.sl = 0;
        a.ah_attr.src_path_bits = 0; a.ah_attr.port_num = ib_port;
        memcpy(a.ah_attr.grh.dgid.raw, peer->gid, 16);
        a.ah_attr.grh.sgid_index = gid_idx; a.ah_attr.grh.hop_limit = 64;
        a.ah_attr.grh.traffic_class = tclass;
        if (ibv_modify_qp(r->qp[i], &a, IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
                          IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER)) die("modify RTR");
        struct ibv_qp_attr b = {0};
        b.qp_state = IBV_QPS_RTS; b.timeout = 16; b.retry_cnt = 7; b.rnr_retry = 7;
        b.sq_psn = mine->qp[i].psn; b.max_rd_atomic = 1;
        if (ibv_modify_qp(r->qp[i], &b, IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
                          IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC)) die("modify RTS");
    }
}

/* ------------------------------------------------------------ server -- */
struct srv_arg { int fd; int idx; };
static pthread_mutex_t srv_lock = PTHREAD_MUTEX_INITIALIZER;

static void *server_thread(void *p) {
    struct srv_arg *sa = p;
    struct conn_info peer, mine;
    if (sock_rw(sa->fd, &peer, sizeof(peer), 0)) { close(sa->fd); free(sa); return NULL; }
    if (peer.nqp > MAX_QP) peer.nqp = MAX_QP;
    struct rdma_ctx r;
    size_t len = (size_t)(region_gb * (1ULL << 30));
    ctx_init(&r, len, peer.nqp, 64, 16);
    fill_info(&r, &mine);
    connect_qps(&r, &mine, &peer);
    if (sock_rw(sa->fd, &mine, sizeof(mine), 1)) goto out;
    pthread_mutex_lock(&srv_lock);
    fprintf(stderr, "[server] client %d up: %u QPs, region %.1f GB\n", sa->idx, peer.nqp, region_gb);
    pthread_mutex_unlock(&srv_lock);
    char c; while (recv(sa->fd, &c, 1, 0) > 0) ;   /* block until the client hangs up */
    pthread_mutex_lock(&srv_lock);
    fprintf(stderr, "[server] client %d gone\n", sa->idx);
    pthread_mutex_unlock(&srv_lock);
out:
    for (int i = 0; i < r.n; i++) ibv_destroy_qp(r.qp[i]);
    ibv_destroy_cq(r.cq); ibv_dereg_mr(r.mr); munmap(r.buf, r.buflen);
    ibv_dealloc_pd(r.pd); ibv_close_device(r.ctx);
    close(sa->fd); free(sa); return NULL;
}

static int run_server(void) {
    int ls = socket(AF_INET, SOCK_STREAM, 0); int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a = {0}; a.sin_family = AF_INET; a.sin_port = htons(port); a.sin_addr.s_addr = INADDR_ANY;
    if (bind(ls, (struct sockaddr *)&a, sizeof(a))) die("bind");
    listen(ls, 16);
    fprintf(stderr, "[server] %s port %d gid %d, waiting\n", dev_name, port, gid_idx);
    int idx = 0;
    while (!stop_flag) {
        int fd = accept(ls, NULL, NULL);
        if (fd < 0) { if (errno == EINTR) continue; die("accept"); }
        struct srv_arg *sa = malloc(sizeof(*sa)); sa->fd = fd; sa->idx = idx++;
        pthread_t th; pthread_create(&th, NULL, server_thread, sa); pthread_detach(th);
    }
    return 0;
}

/* ------------------------------------------------------- group barrier -- */
/* -G L:<port>:<n>  this member leads a group of n (itself included)
 * -G F:<host>:<port>  this member joins the group led from <host>
 * At the end of every step each follower sends 'D' and waits for 'G'; the
 * leader waits for its own step and every follower's 'D', then sends 'G'. */
static int grp_leader = 0, grp_n = 1, grp_fd[MAX_MEMBERS], grp_nfd = 0;

static void group_setup(void) {
    if (!group_spec) return;
    char spec[256]; snprintf(spec, sizeof(spec), "%s", group_spec);
    char *a = strtok(spec, ":"), *b = strtok(NULL, ":"), *c = strtok(NULL, ":");
    if (!a || !b || !c) { fprintf(stderr, "bad -G\n"); exit(1); }
    if (a[0] == 'L') {
        grp_leader = 1; grp_n = atoi(c);
        int ls = socket(AF_INET, SOCK_STREAM, 0), one = 1;
        setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in sa = {0}; sa.sin_family = AF_INET; sa.sin_port = htons(atoi(b)); sa.sin_addr.s_addr = INADDR_ANY;
        if (bind(ls, (struct sockaddr *)&sa, sizeof(sa))) die("group bind");
        listen(ls, MAX_MEMBERS);
        while (grp_nfd < grp_n - 1) {
            int fd = accept(ls, NULL, NULL);
            if (fd < 0) { if (errno == EINTR && !stop_flag) continue; die("group accept"); }
            setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
            grp_fd[grp_nfd++] = fd;
        }
        close(ls);
        fprintf(stderr, "[client %s] group of %d assembled\n", client_id, grp_n);
    } else {
        struct addrinfo hints = {0}, *res;
        hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
        if (getaddrinfo(b, c, &hints, &res)) { fprintf(stderr, "group host %s?\n", b); exit(1); }
        int fd = -1, one = 1;
        for (int tries = 0; tries < 300 && !stop_flag; tries++) {   /* the leader may come up later */
            fd = socket(AF_INET, SOCK_STREAM, 0);
            if (connect(fd, res->ai_addr, res->ai_addrlen) == 0) break;
            close(fd); fd = -1; usleep(100000);
        }
        freeaddrinfo(res);
        if (fd < 0) { fprintf(stderr, "group: cannot reach leader\n"); exit(1); }
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        grp_fd[grp_nfd++] = fd;
    }
}

static void group_barrier(void) {
    char ch;
    if (!group_spec) return;
    if (grp_leader) {
        for (int i = 0; i < grp_nfd; i++) if (sock_rw(grp_fd[i], &ch, 1, 0)) die("group read");
        ch = 'G';
        for (int i = 0; i < grp_nfd; i++) if (sock_rw(grp_fd[i], &ch, 1, 1)) die("group write");
    } else {
        ch = 'D'; if (sock_rw(grp_fd[0], &ch, 1, 1)) die("group write");
        if (sock_rw(grp_fd[0], &ch, 1, 0)) die("group read");
    }
}

/* ------------------------------------------------------------ client -- */
struct req {
    double t_arr;            /* arrival, s from start */
    double t_start, t_end;   /* monotonic; 0 = not yet */
    size_t bytes; long tokens;
    size_t nslices, posted, done;
};

static struct req *reqs;
static long nreq;

static void load_schedule(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) die("schedule");
    long cap = 1024; reqs = calloc(cap, sizeof(*reqs));
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        double t; unsigned long long b; long tok = 0;
        if (line[0] == '#') continue;
        int k = sscanf(line, "%lf %llu %ld", &t, &b, &tok);
        if (k < 2 || b == 0) continue;
        if (nreq == cap) { cap *= 2; reqs = realloc(reqs, cap * sizeof(*reqs)); memset(reqs + nreq, 0, (cap - nreq) * sizeof(*reqs)); }
        reqs[nreq].t_arr = t; reqs[nreq].bytes = b; reqs[nreq].tokens = tok;
        reqs[nreq].nslices = (b + slice - 1) / slice;
        nreq++;
    }
    fclose(f);
}

static void log_req(FILE *out, long i, double mono0, double wall0, const char *status) {
    struct req *q = &reqs[i];
    double ta = wall0 + q->t_arr;
    double ts = q->t_start ? wall0 + (q->t_start - mono0) : 0;
    double te = q->t_end ? wall0 + (q->t_end - mono0) : 0;
    double qms = q->t_start ? (q->t_start - mono0 - q->t_arr) * 1e3 : 0;
    double xms = q->t_end ? (q->t_end - q->t_start) * 1e3 : 0;
    double tms = q->t_end ? (q->t_end - mono0 - q->t_arr) * 1e3 : 0;
    size_t moved = q->done * slice < q->bytes ? q->done * slice : q->bytes;
    fprintf(out, "%s,%ld,%.6f,%.6f,%.6f,%ld,%zu,%zu,%.3f,%.3f,%.3f,%.3f,%s\n", client_id, i, ta, ts, te, q->tokens,
            q->bytes, moved, qms, xms, tms, xms > 0 ? q->bytes * 8.0 / xms / 1e6 : 0.0, status);
}

/* Post and complete slices for the requests in service until none is left in
 * service or the deadline passes. Returns 0, or -1 on a work-completion error. */
struct engine {
    struct rdma_ctx *r; struct conn_info *peer; size_t off, len;
    int inflight[MAX_QP];
    long active[256]; int nactive, rr;
};

static int engine_step(struct engine *e, int may_post, double *last_done) {
    struct ibv_wc wc[64];
    if (may_post && e->nactive > 0) {
        int progressed = 1;
        while (progressed) {
            progressed = 0;
            for (int q = 0; q < e->r->n; q++) {
                if (e->inflight[q] >= depth) continue;
                /* the next request in service that still has slices, round-robin */
                long ridx = -1;
                for (int k = 0; k < e->nactive; k++) {
                    int a = (e->rr + k) % e->nactive;
                    if (reqs[e->active[a]].posted < reqs[e->active[a]].nslices) { ridx = e->active[a]; e->rr = (a + 1) % e->nactive; break; }
                }
                if (ridx < 0) break;
                struct req *rq = &reqs[ridx];
                size_t this = slice;
                if (rq->posted == rq->nslices - 1) { size_t rem = rq->bytes - rq->posted * slice; if (rem) this = rem; }
                if (e->off + this > e->len) e->off = 0;
                struct ibv_sge sge = { .addr = (uint64_t)(uintptr_t)e->r->buf + e->off, .length = (uint32_t)this, .lkey = e->r->mr->lkey };
                struct ibv_send_wr wr = {0}, *bad;
                wr.wr_id = ((uint64_t)ridx << 8) | (uint64_t)q;
                wr.sg_list = &sge; wr.num_sge = 1; wr.opcode = IBV_WR_RDMA_WRITE; wr.send_flags = IBV_SEND_SIGNALED;
                wr.wr.rdma.remote_addr = e->peer->addr + e->off; wr.wr.rdma.rkey = e->peer->rkey;
                if (ibv_post_send(e->r->qp[q], &wr, &bad)) die("ibv_post_send");
                e->off += this; e->inflight[q]++; rq->posted++; progressed = 1;
            }
        }
    }
    int n = ibv_poll_cq(e->r->cq, 64, wc);
    if (n < 0) die("ibv_poll_cq");
    double t = n ? now_mono() : 0;
    for (int i = 0; i < n; i++) {
        if (wc[i].status != IBV_WC_SUCCESS) {
            fprintf(stderr, "[client %s] WC error %s\n", client_id, ibv_wc_status_str(wc[i].status));
            return -1;
        }
        int q = wc[i].wr_id & 0xff; long ridx = (long)(wc[i].wr_id >> 8);
        e->inflight[q]--;
        reqs[ridx].done++;
        if (reqs[ridx].done == reqs[ridx].nslices) {
            reqs[ridx].t_end = t;
            for (int k = 0; k < e->nactive; k++)
                if (e->active[k] == ridx) { e->active[k] = e->active[--e->nactive]; if (e->rr >= e->nactive) e->rr = 0; break; }
            if (last_done) *last_done = t;
        }
    }
    return 0;
}

static int inflight_total(struct engine *e) { int s = 0; for (int q = 0; q < e->r->n; q++) s += e->inflight[q]; return s; }

static int run_client(const char *server_ip) {
    size_t len = (size_t)(region_gb * (1ULL << 30));
    struct rdma_ctx r;
    ctx_init(&r, len, nqp, nqp * depth * 2, depth * 2);
    struct conn_info mine, peer; fill_info(&r, &mine);

    /* The control connection only exchanges QP numbers; the data path is
     * set by the two devices' GIDs. Given the server's management name it
     * stays off the VF, where an idle TCP connection would count as a live
     * TCP flow-set at the receiver for the whole run. */
    struct addrinfo hints = {0}, *res;
    char portstr[16]; snprintf(portstr, sizeof(portstr), "%d", port);
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(server_ip, portstr, &hints, &res)) { fprintf(stderr, "cannot resolve %s\n", server_ip); return 1; }
    int fd = -1, connected = 0;
    for (int tries = 0; tries < 100 && !stop_flag; tries++) {   /* the server may still be starting */
        fd = socket(AF_INET, SOCK_STREAM, 0);
        if (connect(fd, res->ai_addr, res->ai_addrlen) == 0) { connected = 1; break; }
        close(fd); usleep(100000);
    }
    freeaddrinfo(res);
    if (!connected) die("connect");
    if (sock_rw(fd, &mine, sizeof(mine), 1) || sock_rw(fd, &peer, sizeof(peer), 0)) die("exchange");
    connect_qps(&r, &mine, &peer);
    if (peer.len < len) len = peer.len;
    group_setup();

    FILE *out = out_path ? fopen(out_path, "w") : stdout;
    if (!out) die("fopen");
    fprintf(out, "id,req,t_arrive,t_start,t_end,tokens,bytes,bytes_moved,queue_ms,xfer_ms,total_ms,xfer_gbps,status\n");
    fflush(out);

    /* wait for the absolute start */
    if (start_epoch > 0) while (!stop_flag && now_wall() < start_epoch) usleep(start_epoch - now_wall() > 0.01 ? 5000 : 100);
    double mono0 = now_mono(), wall0 = now_wall(), deadline = mono0 + duration;
    struct engine e = { .r = &r, .peer = &peer, .off = 0, .len = len };
    int err = 0;

    if (sched_path) {
        fprintf(stderr, "[client %s] %s -> %s: open loop, %ld requests, %d QPs depth %d slice %zu, <= %d in service, %.0fs\n",
                client_id, dev_name, server_ip, nreq, nqp, depth, slice, max_active, duration);
        long next = 0;    /* next request to start, in arrival order */
        while (!stop_flag && !err) {
            double t = now_mono();
            int may_post = t < deadline;
            /* start what has arrived, up to max_active in service */
            while (may_post && next < nreq && mono0 + reqs[next].t_arr <= t && e.nactive < max_active) {
                reqs[next].t_start = t; e.active[e.nactive++] = next; next++;
            }
            if (!may_post && inflight_total(&e) == 0) break;
            if (may_post && e.nactive == 0) {
                if (next >= nreq) break;
                double wait = mono0 + reqs[next].t_arr - t;   /* idle until the next arrival */
                if (wait > 0.0002) usleep(wait > 0.01 ? 5000 : 100);
                continue;
            }
            if (engine_step(&e, may_post, NULL)) err = 1;
        }
        /* drain whatever is still in flight, then account for every request */
        double drain_until = now_mono() + 5;
        while (!err && inflight_total(&e) > 0 && now_mono() < drain_until) if (engine_step(&e, 0, NULL)) err = 1;
        long nd = 0;
        for (long i = 0; i < nreq; i++) {
            if (reqs[i].t_arr > duration) break;
            const char *st = reqs[i].t_end ? "ok" : reqs[i].t_start ? "unfinished" : "queued";
            if (reqs[i].t_end) nd++;
            log_req(out, i, mono0, wall0, st);
        }
        fprintf(stderr, "[client %s] done: %ld of %ld requests finished in %.1fs%s\n", client_id, nd, next, now_mono() - mono0, err ? " (WC error)" : "");
    } else {
        long steps = 0; unsigned long long bytes = 0; double compute = 0;
        if (sscanf(iter_spec, "%ld:%llu:%lf", &steps, &bytes, &compute) != 3 || steps <= 0) { fprintf(stderr, "bad -I\n"); return 1; }
        nreq = steps; reqs = calloc(steps, sizeof(*reqs));
        fprintf(stderr, "[client %s] %s -> %s: %ld steps of %.2f GB, compute %.1fs, %d QPs depth %d%s\n", client_id, dev_name,
                server_ip, steps, bytes / 1e9, compute, nqp, depth, group_spec ? ", grouped" : "");
        double t_step = mono0;
        for (long s = 0; s < steps && !stop_flag && !err; s++) {
            reqs[s].bytes = bytes; reqs[s].nslices = (bytes + slice - 1) / slice;
            reqs[s].t_arr = t_step - mono0; reqs[s].t_start = now_mono();
            e.active[0] = s; e.nactive = 1;
            while (!stop_flag && !err && reqs[s].t_end == 0 && now_mono() < deadline) if (engine_step(&e, 1, NULL)) err = 1;
            if (reqs[s].t_end == 0) { log_req(out, s, mono0, wall0, "unfinished"); break; }
            double own = reqs[s].t_end;
            group_barrier();
            double all = now_mono();
            log_req(out, s, mono0, wall0, "ok");
            fprintf(out, "#step,%ld,own_ms,%.3f,group_ms,%.3f\n", s, (own - reqs[s].t_start) * 1e3, (all - reqs[s].t_start) * 1e3);
            fflush(out);
            /* the compute phase, then the next step */
            double until = all + compute;
            while (!stop_flag && now_mono() < until) usleep(1000);
            t_step = now_mono();
        }
        fprintf(stderr, "[client %s] done: %ld steps in %.1fs%s\n", client_id, steps, now_mono() - mono0, err ? " (WC error)" : "");
    }
    if (out != stdout) fclose(out);
    close(fd);
    for (int i = 0; i < r.n; i++) ibv_destroy_qp(r.qp[i]);
    ibv_destroy_cq(r.cq); ibv_dereg_mr(r.mr); munmap(r.buf, r.buflen);
    ibv_dealloc_pd(r.pd); ibv_close_device(r.ctx);
    return err;
}

int main(int argc, char **argv) {
    int is_server = 0; const char *server_ip = NULL; int opt;
    while ((opt = getopt(argc, argv, "sc:d:p:x:q:D:z:S:M:I:G:A:t:C:o:i:b:m:h")) != -1) {
        switch (opt) {
        case 's': is_server = 1; break;
        case 'c': server_ip = optarg; break;
        case 'd': dev_name = optarg; break;
        case 'p': port = atoi(optarg); break;
        case 'x': gid_idx = atoi(optarg); break;
        case 'q': nqp = atoi(optarg); break;
        case 'D': depth = atoi(optarg); break;
        case 'z': slice = strtoull(optarg, NULL, 10); break;
        case 'S': sched_path = optarg; break;
        case 'M': max_active = atoi(optarg); break;
        case 'I': iter_spec = optarg; break;
        case 'G': group_spec = optarg; break;
        case 'A': start_epoch = atof(optarg); break;
        case 't': duration = atof(optarg); break;
        case 'C': tclass = atoi(optarg); break;
        case 'o': out_path = optarg; break;
        case 'i': client_id = optarg; break;
        case 'b': region_gb = atof(optarg); break;
        case 'm': want_mtu = atoi(optarg); break;
        default:
            fprintf(stderr, "usage: kvfetch -s | -c <ip>  -d <dev> [-p port] [-x gid] [-q nqp] [-D depth] [-z slice]\n"
                            "       (-S sched [-M max_in_service] | -I steps:bytes:compute_s [-G L:port:n|F:host:port])\n"
                            "       [-A start_epoch] [-t sec] [-C tclass] [-o csv] [-i id] [-b regionGB] [-m mtu]\n");
            return 1;
        }
    }
    if (!dev_name || (!is_server && !server_ip)) { fprintf(stderr, "need -d and -s or -c\n"); return 1; }
    if (!is_server && !sched_path && !iter_spec) { fprintf(stderr, "a client needs -S or -I\n"); return 1; }
    if (nqp > MAX_QP) nqp = MAX_QP;
    if (max_active > 256) max_active = 256;
    srand((unsigned)getpid());
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);
    if (sched_path) load_schedule(sched_path);
    return is_server ? run_server() : run_client(server_ip);
}
