/* tcp_blast: TCP load that starts at an ABSOLUTE instant.
 *
 *   sender: tcp_blast -c <dst_ip> -p <port> -B <src_ip> [-I <dev>] -P <streams>
 *                     -t <secs> [-S <epoch_seconds>] [-C <cc>]
 *   sink:   tcp_blast -r -p <port> -B <ip>          (accepts and discards)
 *
 * Why not iperf3. A flow-set that joins mid-run has to put its first byte on
 * the wire AT the scenario's event instant, because every convergence number
 * is measured from that instant. perftest does this with --start_at; iperf3
 * (3.20) has no absolute or delayed start, so the runner could only "sleep
 * until the slot, then exec iperf3" - which puts iperf3's whole startup AFTER
 * the event. Measured 2026-08-31: 521 ms from exec to the first byte reaching
 * the receiver for one client, ~1.1 s with eight starting at once. The
 * receiver saw literally zero bytes for that long, the newcomer's fence sat
 * on its floor with nothing to probe against, and the run was charged for it.
 *
 * So: connect every stream FIRST, then sleep to the absolute start, then
 * blast. The handshake cost lands before the event, where it belongs.
 *
 * The connect happens PRECONNECT_S before the start, not at launch: a VF whose
 * MTU disagrees with its representor drops large segments on a TCP connection
 * that has been idle more than ~10 s (ops_notes), and the runner launches its
 * clients ~20 s ahead of T0. Two seconds is far more than the 0.5 s a
 * handshake needs and far less than that idle window.
 *
 * Build: gcc -O2 -pthread -o tcp_blast tcp_blast.c
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define BUFSZ (1 << 20)
#define PRECONNECT_S 2.0

static volatile int stop;
static void on_sig(int s) { (void)s; stop = 1; }

static double now_s(void)
{
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

struct w { int fd; double secs, start_at; long long sent; };

static void *sender(void *arg)
{
    struct w *w = arg;
    char *buf = malloc(BUFSZ);
    memset(buf, 0x5a, BUFSZ);
    /* every stream is already connected; wait out the last of the gap here so
     * all of them leave the gate together */
    while (!stop && w->start_at > 0 && now_s() < w->start_at) {
        double d = w->start_at - now_s();
        struct timespec ts = { (time_t)d, (long)((d - (time_t)d) * 1e9) };
        if (d > 0.0005) nanosleep(&ts, NULL);
    }
    double end = now_s() + w->secs;
    while (!stop && now_s() < end) {
        ssize_t n = send(w->fd, buf, BUFSZ, MSG_NOSIGNAL);
        if (n < 0) { if (errno == EINTR) continue; break; }
        w->sent += n;
    }
    close(w->fd);
    free(buf);
    return NULL;
}

static void *sink(void *arg)
{
    int fd = *(int *)arg;
    char *buf = malloc(BUFSZ);
    while (!stop && recv(fd, buf, BUFSZ, 0) > 0) ;
    close(fd); free(buf); free(arg);
    return NULL;
}

int main(int argc, char **argv)
{
    const char *dst = NULL, *src = NULL, *dev = NULL, *cc = NULL;
    int port = 5301, np = 1, rx = 0, o;
    double secs = 10, start_at = 0;

    while ((o = getopt(argc, argv, "c:p:B:I:P:t:S:C:r")) != -1) switch (o) {
        case 'c': dst = optarg; break; case 'p': port = atoi(optarg); break;
        case 'B': src = optarg; break; case 'I': dev = optarg; break;
        case 'P': np = atoi(optarg); break; case 't': secs = atof(optarg); break;
        case 'S': start_at = atof(optarg); break; case 'C': cc = optarg; break;
        case 'r': rx = 1; break;
        default: fprintf(stderr, "usage: see header\n"); return 2; }
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);

    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);

    if (rx) {
        int ls = socket(AF_INET, SOCK_STREAM, 0), one = 1;
        setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        a.sin_addr.s_addr = src ? inet_addr(src) : INADDR_ANY;
        if (bind(ls, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("bind"); return 1; }
        listen(ls, 64);
        while (!stop) {
            int *c = malloc(sizeof(int));
            if ((*c = accept(ls, NULL, NULL)) < 0) { free(c); break; }
            pthread_t th; pthread_create(&th, NULL, sink, c); pthread_detach(th);
        }
        return 0;
    }
    if (!dst || !src) { fprintf(stderr, "need -c and -B\n"); return 2; }

    /* connect late enough that the sockets are not left idle for the runner's
     * whole 20 s lead-in, early enough that every handshake is done */
    while (!stop && start_at > 0 && now_s() < start_at - PRECONNECT_S)
        usleep(20000);

    struct w *ws = calloc(np, sizeof(*ws));
    pthread_t *th = calloc(np, sizeof(*th));
    for (int i = 0; i < np; i++) {
        int fd = socket(AF_INET, SOCK_STREAM, 0), one = 1, sz = 16 << 20;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
        if (dev) setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, dev, strlen(dev));
        if (cc) setsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, cc, strlen(cc));
        struct sockaddr_in b = {0};
        b.sin_family = AF_INET; b.sin_addr.s_addr = inet_addr(src);
        if (bind(fd, (struct sockaddr *)&b, sizeof(b)) < 0) { perror("bind"); return 1; }
        a.sin_addr.s_addr = inet_addr(dst);
        if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("connect"); return 1; }
        ws[i].fd = fd; ws[i].secs = secs; ws[i].start_at = start_at;
    }
    /* connected: from here the only thing left before the event is the wait */
    double t0 = start_at > 0 ? start_at : now_s();
    for (int i = 0; i < np; i++) pthread_create(&th[i], NULL, sender, &ws[i]);
    long long tot = 0;
    for (int i = 0; i < np; i++) { pthread_join(th[i], NULL); tot += ws[i].sent; }
    double el = now_s() - t0;
    printf("%.3f Gb/s goodput over %.2f s (%lld bytes, %d streams)\n",
           tot * 8.0 / el / 1e9, el, tot, np);
    return 0;
}
