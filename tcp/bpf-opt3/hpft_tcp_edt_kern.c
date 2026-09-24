// SPDX-License-Identifier: GPL-2.0
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/if_vlan.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/pkt_cls.h>

#include "hpft_bpf_helpers.h"

/* ======================================================================
 * HyperFront TCP executor: host fq+edt, one clock per connection
 *
 * HyperFront's unit is the FLOW SET (one source VM, one destination VM,
 * one class); the sender agent writes the flow set's rate R into
 * hpft_pair_cfg. The tenant's congestion control is per CONNECTION and
 * this program never touches it. The flow set is capped at R and R is
 * split among its connections by the ONE rule both executors use (design
 * v4 section 6.1): each connection gets the share of the set's allowance
 * that its own congestion-control quota is of the sum of the quotas,
 *
 *      r_i = c_i * (R + P/T) / sum_j c_j
 *
 * where P is the set's pool (bytes; the integral of R minus what the set
 * was allowed to send, bounded to a quarter of a millisecond of R either
 * way) and T is one millisecond. All connections of a set share one path
 * and one RTT, so the quotas are proportional to the windows, and the
 * windows w_i = snd_cwnd * mss, read from the socket on the way past, are
 * what stands in for c_i. A connection never exceeds its own CC (it cannot
 * put more than cwnd in flight), so nothing here raises anyone; HyperFront
 * only caps the set.
 *
 * NOTHING ON THE PACKET PATH TAKES A LOCK. The earlier version summed the
 * windows under a per-set spin lock, which is a lock every connection of
 * the set contends for on every packet and does not scale to a VPC's worth
 * of connections. The sum is now what it is on the DPA: each connection
 * adds its window into the set's running sum ONCE PER EPOCH (an atomic
 * add), and the sum an epoch ends with is the divisor for the next one -
 * so the divisor is up to an epoch stale, and the pool is what makes that
 * harmless. If the divisor is off by a factor k the set sends R/k, the
 * pool moves, and it settles exactly where (R + P/T)/sum equals R over the
 * true sum: the k cancels and every connection lands on c_i R / sum c_j.
 * The pool itself is a per-packet atomic subtract (the connection pays for
 * the stretch it has just covered, at the rate it was paced) and a refill
 * that whichever packet wins a compare-and-swap on the refill clock adds
 * for the stretch since the last refill. The epoch roll is one
 * compare-and-swap on the epoch clock, so exactly one packet rolls it and
 * clamps the pool there. What a lost race can cost is bounded by one
 * epoch of one connection's charges, and the pool's own bound covers it.
 *
 * Each connection has its own earliest-departure clock. That is what
 * makes r_i a per-connection quantity on the wire, and it is also what
 * keeps one connection's shaping debt from becoming another's delay or
 * loss.
 * ====================================================================== */

#define HPFT_MAX_VNICS 4096
#define HPFT_MAX_PAIRS 16384
#define HPFT_NSEC_PER_SEC 1000000000ULL
/* Per-wire-frame bytes the shaper must charge for but skb->len never
 * counts: 8 B preamble+SFD, 12 B inter-packet gap, 4 B FCS. */
#define HPFT_FRAME_OVERHEAD 24ULL
/* Max shaping debt one connection may carry, as future-stamp distance.
 * Beyond it, packets are dropped (policer tail on the EDT shaper). The
 * drop is a backstop against unbounded debt from a CC that never yields;
 * it is per connection, so a deep-debt bulk connection only ever drops its
 * own packets. Design v4 6.3 lists it as the TCP executor's boundary. */
#define HPFT_DEBT_CAP_NS (200ULL * 1000 * 1000)
/* One epoch of the flow set's denominator and of its pool bookkeeping: a
 * millisecond, the same tick the RDMA executor runs on. The pool is bounded
 * to a quarter of it, so the epoch must not be much longer than the
 * bound: a refill that arrives once per epoch against a pool a quarter of
 * an epoch deep would pin the pool at its floor for most of every epoch. */
#define HPFT_EPOCH_NS 1000000ULL
/* How far from an equal share one connection's share may be taken by the
 * proportional law, either way, as a fraction NUM/DEN. All connections of a
 * flow set share one path, so a bulk connection has no reason to differ from
 * its neighbours by more than this; what is wider than the band comes from a
 * window our own shaping froze. See the long note where it is applied.
 *
 * The band has to be narrow enough to BREAK the trap it is there for, not
 * merely to bound it. At a factor of two a connection whose window froze
 * settles at the floor - exactly half of its neighbours - and stays there for
 * the rest of the run, because half of an equal share is still too little to
 * grow the window back: measured on E2.2 (2026-09-24), five of ten new-tenant
 * connections sat at 0.64 G against 1.29 G with the same retransmission
 * count, and the flow set delivered 61% of its share. The share is a CEILING
 * (r_i is met by min(the CC's own pacing, it)), so a connection that cannot
 * use what this hands it simply does not, and the pool passes the remainder
 * to its neighbours - which is why erring narrow costs nothing and erring
 * wide costs a stuck connection. */
#define HPFT_SHARE_BAND_NUM 5ULL
#define HPFT_SHARE_BAND_DEN 4ULL
/* How long a connection counts as live for the purpose of that band. The
 * epoch's own count is of the connections that sent in the last millisecond,
 * which is far fewer than the connections a flow set has when they are
 * request/response: one that is waiting for its next request sends nothing for
 * a round trip. Dividing by that count makes an equal share look bigger than
 * it is and the floor built from it too high - measured 2026-09-18, four HTTP
 * flow sets of 256 connections each overran their permitted rate by 35%. This
 * window is long enough to see every connection that is doing anything at all,
 * and counting one that has just stopped only makes the share look smaller and
 * the floor lower, which is the harmless direction. */
#define HPFT_LIVE_EPOCHS 128U
/* A connection with no readable socket (no tcp_sock on the skb) has no
 * window to speak of: it is shaped at an equal share of R over the
 * connections seen last epoch, never at R itself. */

struct hpft_vlan_hdr {
    __u16 h_vlan_TCI;
    __u16 h_vlan_encapsulated_proto;
};

/* written by the pace shim: the flow set's rate R. rate_bps == 0 means
 * "not shaped", which is the tenant-CC-alone arm. burst_bytes is per
 * CONNECTION (one TSO super-packet is the least a clock can hand out at
 * once, so it is also the natural burst). flags are unused. */
struct hpft_rate_cfg {
    __u64 rate_bps;
    __u64 generation;
    __u32 burst_bytes;
    __u32 flags;
};

/* per flow set: the divisor of the law, rebuilt once per epoch from what the
 * connections reported during the previous one, and the pool. No lock: the
 * two clocks are advanced by compare-and-swap, the sums by atomic add, the
 * pool by atomic subtract. Layout is mirrored by tcp_shaper_lib.pack_pair_state. */
struct hpft_pair_state {
    __u64 epoch_ns;     /* start of the current epoch; CAS-rolled */
    __u64 tok_ns;       /* when the pool was last refilled; CAS-advanced */
    __u64 sum_cur;      /* windows reported in this epoch (atomic add) */
    __u64 sum_prev;     /* windows reported in the previous epoch: the divisor */
    __s64 tok;          /* the pool, bytes; charged per packet, refilled per packet, clamped per epoch */
    __u64 generation;   /* cfg generation last seen (diag) */
    __u32 epoch;        /* epoch counter; a connection reports once per */
    __u32 n_cur;        /* connections reported in this epoch (atomic add) */
    __u32 n_prev;       /* connections in the previous epoch */
    __u32 shots;        /* packets dropped at a connection's debt cap (diag) */
    __u32 nosock;       /* packets that carried no tcp_sock (diag) */
    __u32 pad0;
    __u64 tx_bytes;     /* wire bytes of the payload segments let through; read by the
                         * pace shim for the sender's per-flow-set report (atomic add) */
    __u32 live_cur;     /* distinct connections that sent in this live window (atomic add) */
    __u32 live_prev;    /* the previous window's count: how many connections the set has */
    __u32 live_win;     /* epoch at which the current live window opened */
    __u32 pad1;
};

/* per connection (5-tuple): its own clock, its own window, and the rate it
 * was last paced at, which is what it pays the pool at */
struct hpft_flow_state {
    __u64 last_send_ns;
    __u64 next_ns;      /* this connection's earliest-departure clock */
    __u64 win;          /* last window read: snd_cwnd * mss */
    __u64 win_ep;       /* the window this connection reported this epoch */
    __u64 paced;        /* r_i last written, bit/s */
    __u32 epoch_seen;   /* pair epoch this connection last reported in */
    __u32 live_seen;    /* live window this connection last counted itself in */
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, HPFT_MAX_VNICS);
    __type(key, __u32);
    __type(value, __u32);
} hpft_ifindex_to_vnic SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, HPFT_MAX_VNICS);
    __type(key, __u32);
    __type(value, __u32);
} hpft_ip_to_vnic SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, HPFT_MAX_PAIRS);
    __type(key, __u64);
    __type(value, struct hpft_rate_cfg);
} hpft_pair_cfg SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, HPFT_MAX_PAIRS);
    __type(key, __u64);
    __type(value, struct hpft_pair_state);
} hpft_pair_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);   /* LRU: dead connections age out */
    __uint(max_entries, 16384);
    __type(key, __u64);
    __type(value, struct hpft_flow_state);
} hpft_flow_state_map SEC(".maps");

static __always_inline __u16 hpft_ntohs(__u16 value)
{
    return __builtin_bswap16(value);
}

static __always_inline __u64 hpft_bytes_to_ns(__u64 bytes, __u64 rate_bps)
{
    if (!rate_bps)
        return 0;
    return (bytes * 8ULL * HPFT_NSEC_PER_SEC) / rate_bps;
}

static __always_inline int hpft_parse_ipv4_tcp(struct __sk_buff *skb,
                                               struct iphdr **iph_out)
{
    void *data = (void *)(long)skb->data;
    void *data_end = (void *)(long)skb->data_end;
    struct ethhdr *eth = data;
    __u64 offset = sizeof(*eth);
    __u16 proto;

    if ((void *)(eth + 1) > data_end)
        return 0;

    proto = hpft_ntohs(eth->h_proto);
    if (proto == ETH_P_8021Q || proto == ETH_P_8021AD) {
        struct hpft_vlan_hdr *vh = data + offset;

        if ((void *)(vh + 1) > data_end)
            return 0;
        proto = hpft_ntohs(vh->h_vlan_encapsulated_proto);
        offset += sizeof(*vh);
    }

    if (proto != ETH_P_IP)
        return 0;

    struct iphdr *iph = data + offset;
    if ((void *)(iph + 1) > data_end)
        return 0;
    if (iph->version != 4 || iph->protocol != IPPROTO_TCP)
        return 0;
    if ((void *)iph + (iph->ihl * 4) > data_end)
        return 0;

    *iph_out = iph;
    return 1;
}

SEC("classifier")
int hpft_tcp_edt(struct __sk_buff *skb)
{
    struct hpft_rate_cfg *cfg;
    struct hpft_pair_state *state;
    struct hpft_flow_state *fs;
    struct hpft_flow_state fs_new;
    struct iphdr *iph;
    struct tcphdr *tcph;
    void *data_end;
    __u32 *src_vnic;
    __u32 *dst_vnic;
    __u32 src_idx = 0;
    __u32 ifindex;
    __u64 pair_key, flow_key;
    __u64 now, wire, win = 0, R, r_i;
    __u64 send_ns, next_ns, burst_ns, min_next_ns, packet_ns;
    __u32 have_tp = 0;

    if (!hpft_parse_ipv4_tcp(skb, &iph))
        return TC_ACT_OK;

    ifindex = skb->ingress_ifindex;
    if (ifindex) {
        src_vnic = bpf_map_lookup_elem(&hpft_ifindex_to_vnic, &ifindex);
        if (src_vnic)
            src_idx = *src_vnic;
    }
    if (!src_idx) {
        src_vnic = bpf_map_lookup_elem(&hpft_ip_to_vnic, &iph->saddr);
        if (!src_vnic)
            return TC_ACT_OK;
        src_idx = *src_vnic;
    }
    dst_vnic = bpf_map_lookup_elem(&hpft_ip_to_vnic, &iph->daddr);
    if (!dst_vnic)
        return TC_ACT_OK;

    pair_key = ((__u64)src_idx << 32) | (__u64)(*dst_vnic);
    cfg = bpf_map_lookup_elem(&hpft_pair_cfg, &pair_key);
    state = bpf_map_lookup_elem(&hpft_pair_state, &pair_key);
    /* no rate = the tenant CC alone; the program is a no-op */
    if (!cfg || !state || !cfg->rate_bps)
        return TC_ACT_OK;

    data_end = (void *)(long)skb->data_end;
    tcph = (void *)iph + (iph->ihl * 4);
    if ((void *)(tcph + 1) > data_end)
        return TC_ACT_OK;

    /* A segment with no payload - a pure acknowledgement, or a bare
     * SYN/FIN/RST - is not this flow set's traffic. On a host that sends
     * to a peer and also receives from it, the receiving side's
     * acknowledgements leave through the same (local VM, remote VM) pair
     * as its own data, but what they carry is the control loop of the
     * connection in the other direction, whose flow set is drawn at the
     * other host. Such a segment adds nothing to the set's sum, pays the
     * pool nothing and is not delayed; a connection that only
     * acknowledges never becomes a member (design 6.1: S sums the flows
     * that are sending). Same header arithmetic as the wire-byte count
     * below: the IP header follows an untagged Ethernet header. */
    if (skb->len <= ETH_HLEN + (__u32)iph->ihl * 4 + (__u32)tcph->doff * 4)
        return TC_ACT_OK;

    now = bpf_ktime_get_ns();
    /* Meter WIRE bytes, not skb bytes: the share is defined against link
     * capacity, skb->len counts a GSO super-packet's headers once and
     * knows nothing of preamble, IPG or FCS (measured 2026-07-27: 8 % in
     * TCP's favour when this was skb->len). */
    {
        __u32 segs = skb->gso_segs ? skb->gso_segs : 1;
        __u32 hdr = ETH_HLEN + (__u32)iph->ihl * 4 + (__u32)tcph->doff * 4;

        wire = (__u64)skb->len + (__u64)(segs - 1) * (__u64)hdr +
               (__u64)segs * HPFT_FRAME_OVERHEAD;
    }

    /* Step 1: read the CC. snd_cwnd * mss is this connection's allowance
     * c_i. The socket is read, never written. */
    {
        struct bpf_sock *sk = skb->sk;

        if (sk) {
            struct bpf_sock *full = bpf_sk_fullsock(sk);

            if (full) {
                struct bpf_tcp_sock *tp = bpf_tcp_sock(full);

                if (tp) {
                    win = (__u64)tp->snd_cwnd * (__u64)tp->mss_cache;
                    have_tp = 1;
                }
            }
        }
    }

    flow_key = ((__u64)(iph->saddr ^ iph->daddr) << 32) |
               ((__u64)tcph->source << 16) | (__u64)tcph->dest;
    flow_key ^= pair_key;
    fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
    if (!fs) {
        /* a connection seen for the first time: its trend starts at its
         * window, and its clock starts now */
        fs_new.last_send_ns = now;
        fs_new.next_ns = now;
        fs_new.win = win;
        fs_new.win_ep = win;
        fs_new.paced = 0;
        fs_new.epoch_seen = 0xffffffffU;
        /* not the current window, so this connection counts itself the first
         * time it is seen */
        fs_new.live_seen = 0xffffffffU;
        bpf_map_update_elem(&hpft_flow_state_map, &flow_key, &fs_new, BPF_ANY);
        fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
        if (!fs)
            return TC_ACT_OK;
    }
    {
        __u64 prev_send = fs->last_send_ns;

        fs->last_send_ns = now;
        if (have_tp)
            fs->win = win;
        else
            win = fs->win;

        R = cfg->rate_bps;
        {
            /* Step 2: the set's bookkeeping, all lock-free.
             *
             * 2a. The epoch roll: exactly one packet per epoch wins the
             * compare-and-swap on the epoch clock and does the bookkeeping
             * of the boundary - the sum this epoch ends with becomes the
             * divisor, the counters restart, and the pool is clamped to
             * its bound. */
            __u64 ep_ns = state->epoch_ns;
            __s64 depth = (__s64)((R * (HPFT_EPOCH_NS / 4)) / (8ULL * HPFT_NSEC_PER_SEC));

            if (now - ep_ns >= HPFT_EPOCH_NS &&
                __sync_val_compare_and_swap(&state->epoch_ns, ep_ns, now) == ep_ns) {
                __s64 t;

                if (now - ep_ns >= 2 * HPFT_EPOCH_NS) {
                    /* the whole set was quiet for an epoch: nobody reported */
                    state->sum_prev = 0;
                    state->n_prev = 0;
                } else {
                    state->sum_prev = state->sum_cur;
                    state->n_prev = state->n_cur;
                }
                state->sum_cur = 0;
                state->n_cur = 0;
                state->epoch++;
                /* the live window is much longer than the epoch, so it rolls
                 * on its own schedule */
                if (state->epoch - state->live_win >= HPFT_LIVE_EPOCHS) {
                    state->live_prev = state->live_cur;
                    state->live_cur = 0;
                    state->live_win = state->epoch;
                }
                t = state->tok;
                if (t > depth)
                    state->tok = depth;
                else if (t < -depth)
                    state->tok = -depth;
            }
            if (!have_tp)
                __sync_fetch_and_add(&state->nosock, 1);
            state->generation = cfg->generation;

            /* 2b. Once per epoch, this connection adds its window into the
             * running sum. One atomic add per connection per epoch, not per
             * packet. */
            if (have_tp && fs->epoch_seen != state->epoch) {
                __u64 t = win ? win : 1;

                fs->win_ep = t;
                fs->epoch_seen = state->epoch;
                __sync_fetch_and_add(&state->sum_cur, t);
                __sync_fetch_and_add(&state->n_cur, 1);
            }
            /* and once per live window, whatever else it did: this is the
             * count of connections the set HAS, not of the ones that happened
             * to be on the wire in the last millisecond */
            if (fs->live_seen != state->live_win) {
                fs->live_seen = state->live_win;
                __sync_fetch_and_add(&state->live_cur, 1);
            }

            /* 2c. The pool. Refill at R for the stretch since the last
             * refill (whoever wins the swap on the refill clock adds it),
             * then this connection pays for the stretch since its own last
             * packet at the rate it was paced over it; a connection that
             * was away longer than an epoch pays for an epoch, not for the
             * time it was not on the wire. */
            {
                __u64 tk_ns = state->tok_ns;
                __u64 dt = now - tk_ns;

                if (dt > HPFT_EPOCH_NS)
                    dt = HPFT_EPOCH_NS;
                if (now > tk_ns &&
                    __sync_val_compare_and_swap(&state->tok_ns, tk_ns, now) == tk_ns)
                    __sync_fetch_and_add(&state->tok, (__s64)((R * dt) / (8ULL * HPFT_NSEC_PER_SEC)));
            }
            {
                __u64 dq = now - prev_send;

                if (dq > HPFT_EPOCH_NS)
                    dq = HPFT_EPOCH_NS;
                if (fs->paced)
                    __sync_fetch_and_sub(&state->tok, (__s64)((fs->paced * dq) / (8ULL * HPFT_NSEC_PER_SEC)));
            }

            /* 2d. This connection's ceiling: its window's share of the
             * set's allowance, the allowance being R plus the pool spread
             * over one epoch. The stored pool may drift past the bound
             * between two rolls; the bound is applied on the read as well. */
            {
                __s64 t = state->tok;
                __u64 allow, sum, num;

                if (t > depth)
                    t = depth;
                else if (t < -depth)
                    t = -depth;
                allow = R;
                if (t >= 0)
                    allow += ((__u64)t * 8ULL * HPFT_NSEC_PER_SEC) / HPFT_EPOCH_NS;
                else {
                    __u64 debt = ((__u64)(-t) * 8ULL * HPFT_NSEC_PER_SEC) / HPFT_EPOCH_NS;

                    allow = allow > debt ? allow - debt : 0;
                }
                sum = state->sum_prev;
                num = fs->win_ep;
                if (!have_tp || !sum || !num) {
                    /* no divisor yet (first epoch of the set), or no window
                     * to read: an equal share over the connections seen
                     * last epoch */
                    __u32 n = state->n_prev ? state->n_prev : 1;

                    r_i = allow / n;
                } else {
                    /* allow * num / sum without overflow: windows are < 2^32,
                     * so scale the ratio to 2^20 first; a window that grew
                     * past the whole previous sum still gets at most the
                     * whole allowance */
                    __u64 ratio = (num << 20) / sum;
                    __u32 nlive;
                    __u64 equal;

                    if (ratio > (1ULL << 20))
                        ratio = 1ULL << 20;
                    r_i = (allow >> 10) * ratio >> 10;

                    /* Hold the share within a band around an equal one.
                     *
                     * The law divides the allowance in proportion to each
                     * connection's congestion-control quota, which assumes the
                     * quota says something about the PATH. Once this shaper
                     * holds a connection below what it would send, that stops
                     * being true: Linux only grows a window while the
                     * connection is cwnd-limited, and a paced connection is
                     * pacing-limited, so the window we read stops moving and
                     * stays where it stood when the shaping began. Nothing
                     * else in the stack is any better, because the shaper
                     * removes nearly all the loss the quota is built from (11
                     * retransmissions per connection against 2000 or more
                     * without it, measured 2026-09-18).
                     *
                     * Left alone that traps a connection: a window frozen
                     * small earns a small rate, a small rate sends little, and
                     * little sending cannot grow the window back. In a
                     * 400-connection flow set, 7 of them ended a run at a
                     * quarter of the others' window and rate with exactly the
                     * same retransmission count - not slower, stuck.
                     *
                     * All connections of a flow set share one path (see the
                     * header), so a real difference between their quotas can
                     * only come from an application that is not offering data,
                     * and such a connection does not need a large share: it
                     * will not use it, and the pool hands the remainder to the
                     * others. Everything wider than the band is an artefact of
                     * our own shaping.
                     *
                     * The equal share is over the connections the set HAS
                     * (live_prev), never over the few that happened to be on
                     * the wire in the last millisecond - see HPFT_LIVE_EPOCHS.
                     * Taking the larger of the two keeps the count on the safe
                     * side: too many connections makes the floor lower and the
                     * band wider, which does nothing; too few makes the floor
                     * higher than a share, and then the floors add up to more
                     * than the allowance and the flow set overruns its
                     * permitted rate.
                     */
                    nlive = state->live_prev;
                    if (nlive < state->n_prev)
                        nlive = state->n_prev;
                    if (!nlive)
                        nlive = 1;
                    equal = allow / nlive;
                    if (r_i < equal * HPFT_SHARE_BAND_DEN / HPFT_SHARE_BAND_NUM)
                        r_i = equal * HPFT_SHARE_BAND_DEN / HPFT_SHARE_BAND_NUM;
                    else if (r_i > equal * HPFT_SHARE_BAND_NUM / HPFT_SHARE_BAND_DEN)
                        r_i = equal * HPFT_SHARE_BAND_NUM / HPFT_SHARE_BAND_DEN;
                }
            }
        }
    }
    if (r_i > R)
        r_i = R;
    if (!r_i)
        r_i = 1;
    fs->paced = r_i;

    /* Step 3: shape this connection to r_i on its own clock. A connection
     * that was idle may send burst_bytes at once (its clock is allowed to
     * lag `now` by that much), which is what keeps a request/response
     * connection from waiting on anything: its own clock never runs
     * ahead of it. */
    packet_ns = hpft_bytes_to_ns(wire, r_i);
    burst_ns = hpft_bytes_to_ns((__u64)cfg->burst_bytes, r_i);
    min_next_ns = now > burst_ns ? now - burst_ns : 0;
    send_ns = fs->next_ns;
    if (send_ns < min_next_ns)
        send_ns = min_next_ns;
    if (send_ns > now + HPFT_DEBT_CAP_NS) {
        /* this connection's own debt beyond the cap: drop rather than
         * stamp ever further out; its CC reads the loss, nobody else's */
        __sync_fetch_and_add(&state->shots, 1);
        return TC_ACT_SHOT;
    }
    next_ns = send_ns + packet_ns;
    if (next_ns < send_ns)
        next_ns = send_ns;
    fs->next_ns = next_ns;
    /* max(): the sock may carry its own EDT stamp (BBR's pacing writes
     * skb->tstamp directly); the later of the two is the send time, i.e.
     * the wire rate is min(the CC's own pacing, our share). */
    if (send_ns > now && send_ns > skb->tstamp)
        skb->tstamp = send_ns;
    /* what this flow set puts on the wire, for the sender agent: it divides
     * the VF's fresh vport total over the VF's flow sets in these proportions
     * (platform notes, section 2) */
    __sync_fetch_and_add(&state->tx_bytes, wire);
    return TC_ACT_OK;
}

char _license[] SEC("license") = "GPL";
