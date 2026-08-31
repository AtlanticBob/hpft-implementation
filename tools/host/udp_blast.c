/* udp_blast: background UDP traffic that HyperFront neither paces nor
 * schedules (ip_other at the receiver). Used by validation V7 to congest
 * the receiver's switch port with traffic the allocator does not own.
 *
 *   sender:  udp_blast -c <dst_ip> -p <port> -B <src_ip> -G <gbps> -t <secs> [-n <workers>]
 *   sink:    udp_blast -r -p <port> -B <ip>                    (discards; Ctrl-C/kill to stop)
 *
 * Each worker owns one connected UDP socket with UDP_SEGMENT (GSO): one
 * sendmmsg call hands the kernel 64 KiB that the NIC splits into 1472-byte
 * datagrams, so a single core pushes >20 Gb/s. Rate is paced per worker
 * with a byte budget refilled from CLOCK_MONOTONIC (500 us slices).
 * Build: gcc -O2 -pthread -o udp_blast udp_blast.c
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/udp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifndef UDP_SEGMENT
#define UDP_SEGMENT 103
#endif
#define SEG 1472
#define BUFSZ (SEG * 44)          /* 64768 B: 44 datagrams per GSO buffer */
#define BATCH 8

static volatile int stop;
static void on_sig(int s) { (void)s; stop = 1; }

struct w { const char *dst, *src; int port; double gbps; double secs; long long sent; };

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

static void *sender(void *arg)
{
    struct w *w = arg;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_addr.s_addr = inet_addr(w->src); a.sin_port = 0;
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("bind"); return NULL; }
    a.sin_addr.s_addr = inet_addr(w->dst); a.sin_port = htons(w->port);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("connect"); return NULL; }
    int seg = SEG, sz = 4 << 20;
    setsockopt(fd, SOL_UDP, UDP_SEGMENT, &seg, sizeof(seg));
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
    static char buf[BUFSZ];
    struct iovec iov[BATCH]; struct mmsghdr msgs[BATCH];
    memset(msgs, 0, sizeof(msgs));
    for (int i = 0; i < BATCH; i++) { iov[i].iov_base = buf; iov[i].iov_len = BUFSZ; msgs[i].msg_hdr.msg_iov = &iov[i]; msgs[i].msg_hdr.msg_iovlen = 1; }
    double bps = w->gbps * 1e9 / 8.0 * 1.0;         /* payload bytes/s; wire adds ~3% */
    double t0 = now_s(), tl = t0, budget = 0;
    while (!stop && now_s() - t0 < w->secs) {
        double t = now_s();
        budget += (t - tl) * bps; tl = t;
        if (budget > bps * 0.002) budget = bps * 0.002;    /* 2 ms burst cap */
        if (budget < BUFSZ) { struct timespec s = {0, 200000}; nanosleep(&s, NULL); continue; }
        int n = (int)(budget / BUFSZ); if (n > BATCH) n = BATCH;
        int r = sendmmsg(fd, msgs, n, 0);
        if (r < 0) { if (errno == EAGAIN || errno == ENOBUFS) { struct timespec s = {0, 100000}; nanosleep(&s, NULL); continue; } perror("sendmmsg"); break; }
        budget -= (double)r * BUFSZ; w->sent += (long long)r * BUFSZ;
    }
    close(fd);
    return NULL;
}

static void sink(const char *ip, int port)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0), sz = 64 << 20;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &sz, sizeof(sz));
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_addr.s_addr = ip ? inet_addr(ip) : INADDR_ANY; a.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("bind"); exit(1); }
    static char buf[64][2048]; struct iovec iov[64]; struct mmsghdr msgs[64];
    memset(msgs, 0, sizeof(msgs));
    for (int i = 0; i < 64; i++) { iov[i].iov_base = buf[i]; iov[i].iov_len = 2048; msgs[i].msg_hdr.msg_iov = &iov[i]; msgs[i].msg_hdr.msg_iovlen = 1; }
    long long pk = 0; double t0 = now_s();
    while (!stop) { int r = recvmmsg(fd, msgs, 64, MSG_WAITFORONE, NULL); if (r > 0) pk += r; }
    printf("sink: %lld datagrams in %.1f s\n", pk, now_s() - t0);
}

int main(int argc, char **argv)
{
    const char *dst = NULL, *src = NULL; int port = 5999, rx = 0, nw = 0, o; double gbps = 10, secs = 10;
    while ((o = getopt(argc, argv, "c:p:B:G:t:n:r")) != -1) switch (o) {
        case 'c': dst = optarg; break; case 'p': port = atoi(optarg); break; case 'B': src = optarg; break;
        case 'G': gbps = atof(optarg); break; case 't': secs = atof(optarg); break; case 'n': nw = atoi(optarg); break;
        case 'r': rx = 1; break; default: fprintf(stderr, "usage: see header\n"); return 2; }
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);
    if (rx) { sink(src, port); return 0; }
    if (!dst || !src) { fprintf(stderr, "need -c dst and -B src\n"); return 2; }
    if (!nw) nw = (int)(gbps / 12.0) + 1;
    struct w *ws = calloc(nw, sizeof(*ws)); pthread_t *th = calloc(nw, sizeof(*th));
    for (int i = 0; i < nw; i++) { ws[i].dst = dst; ws[i].src = src; ws[i].port = port; ws[i].gbps = gbps / nw; ws[i].secs = secs; pthread_create(&th[i], NULL, sender, &ws[i]); }
    long long tot = 0; for (int i = 0; i < nw; i++) { pthread_join(th[i], NULL); tot += ws[i].sent; }
    printf("udp_blast: %d workers, %.2f Gb/s asked, %.2f Gb/s payload sent over %.1f s\n", nw, gbps, tot * 8.0 / secs / 1e9, secs);
    return 0;
}
