/* tcp_burst: a TCP "flapping neighbour" - N connections that send flat out
 * for on_ms, then send nothing for off_ms, repeatedly. Used by validation
 * V8 to see how much of a neighbour's on/off pattern reaches a stable
 * tenant sharing the same VM (isolation of control inputs).
 *   sink:   tcp_burst -r -p <port> -B <ip> -n <conns>
 *   client: tcp_burst -c <dst_ip> -p <port> -B <src_ip> -n <conns> -o <on_ms> -f <off_ms> -t <secs>
 * Build: gcc -O2 -pthread -o tcp_burst tcp_burst.c */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <math.h>
#include <unistd.h>
static volatile int stop;
static void on_sig(int s) { (void)s; stop = 1; }
static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
struct w { const char *dst, *src; int port, on_ms, off_ms; double secs, t0; long long sent; };
static void *sender(void *a)
{
    struct w *w = a; int fd = socket(AF_INET, SOCK_STREAM, 0), one = 1, sz = 8 << 20;
    struct sockaddr_in s = {0}; s.sin_family = AF_INET; s.sin_addr.s_addr = inet_addr(w->src);
    if (bind(fd, (struct sockaddr *)&s, sizeof(s)) < 0) { perror("bind"); return NULL; }
    s.sin_addr.s_addr = inet_addr(w->dst); s.sin_port = htons(w->port);
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz)); setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    if (connect(fd, (struct sockaddr *)&s, sizeof(s)) < 0) { perror("connect"); return NULL; }
    static char buf[1 << 20];
    double period = (w->on_ms + w->off_ms) / 1e3;
    while (!stop && now_s() - w->t0 < w->secs) {
        double ph = fmod(now_s() - w->t0, period);           /* all threads share the phase */
        if (ph < w->on_ms / 1e3) { ssize_t r = send(fd, buf, sizeof buf, 0); if (r < 0) break; w->sent += r; }
        else { struct timespec t = {0, 200000}; nanosleep(&t, NULL); }
    }
    close(fd); return NULL;
}
static void *sink(void *a) { int fd = *(int *)a; static __thread char b[1 << 20]; while (!stop && recv(fd, b, sizeof b, 0) > 0) ; close(fd); return NULL; }
int main(int argc, char **argv)
{
    const char *dst = NULL, *src = NULL; int port = 5800, rx = 0, n = 4, on = 250, off = 250, o; double secs = 10;
    while ((o = getopt(argc, argv, "c:p:B:n:o:f:t:r")) != -1) switch (o) {
        case 'c': dst = optarg; break; case 'p': port = atoi(optarg); break; case 'B': src = optarg; break; case 'n': n = atoi(optarg); break;
        case 'o': on = atoi(optarg); break; case 'f': off = atoi(optarg); break; case 't': secs = atof(optarg); break; case 'r': rx = 1; break;
        default: return 2; }
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig); signal(SIGPIPE, SIG_IGN);
    if (rx) {
        int ls = socket(AF_INET, SOCK_STREAM, 0), one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in s = {0}; s.sin_family = AF_INET; s.sin_addr.s_addr = src ? inet_addr(src) : INADDR_ANY; s.sin_port = htons(port);
        if (bind(ls, (struct sockaddr *)&s, sizeof(s)) < 0 || listen(ls, 64) < 0) { perror("listen"); return 1; }
        pthread_t th[256]; int fds[256], k = 0;
        while (!stop && k < 256) { int fd = accept(ls, NULL, NULL); if (fd < 0) break; fds[k] = fd; pthread_create(&th[k], NULL, sink, &fds[k]); k++; }
        return 0;
    }
    if (!dst || !src) { fprintf(stderr, "need -c and -B\n"); return 2; }
    struct w *ws = calloc(n, sizeof *ws); pthread_t *th = calloc(n, sizeof *th); double t0 = now_s() + 0.5;
    for (int i = 0; i < n; i++) { ws[i] = (struct w){dst, src, port, on, off, secs, t0, 0}; pthread_create(&th[i], NULL, sender, &ws[i]); }
    long long tot = 0; for (int i = 0; i < n; i++) { pthread_join(th[i], NULL); tot += ws[i].sent; }
    printf("tcp_burst: %d conns on %d ms / off %d ms, %.2f Gb/s average over %.0f s\n", n, on, off, tot * 8.0 / secs / 1e9, secs);
    return 0;
}
