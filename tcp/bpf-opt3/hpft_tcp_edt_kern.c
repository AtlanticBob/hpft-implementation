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
 * this program never touches it. HyperFront here is ONE TOKEN BUCKET PER
 * FLOW SET filled at R (design v4 section 6): the connections draw at the
 * rate their own CC produces, and when the set asks for more than R the
 * bucket is shared in proportion to how fast each one draws. A window CC
 * draws at cwnd/RTT, and all connections of a set share one path, so the
 * proportion is the proportion of windows:
 *
 *      r_i = R * w_i / sum_j w_j       (j over connections that sent)
 *
 * with w_i = snd_cwnd * mss read from the socket on the way past. A
 * connection never exceeds its own CC (it cannot put more than cwnd in
 * flight), so nothing here raises anyone; HyperFront only caps the set.
 *
 * Each connection has its own earliest-departure clock. That is what
 * makes r_i a per-connection quantity on the wire, and it is also what
 * keeps one connection's shaping debt from becoming another's delay or
 * loss: with one clock per flow set a bulk connection's backlog delayed a
 * request/response connection's packets and dropped them at the debt cap
 * in its stead. The sum over the set is bounded by R by construction.
 *
 * The denominator is rebuilt every epoch (10 ms, one HyperFront period)
 * from the connections that actually sent in the previous epoch, so a
 * connection that stops sending drops out of the split by itself; there
 * is no member list to maintain and nothing to compact.
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
 * it is now per connection, so a deep-debt bulk connection only ever drops
 * its own packets. */
#define HPFT_DEBT_CAP_NS (200ULL * 1000 * 1000)
/* One epoch of the flow set's denominator: one HyperFront period. */
#define HPFT_EPOCH_NS 10000000ULL
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

/* per flow set: the denominator of the distribution law, rebuilt once per
 * epoch from what the connections reported during the previous one */
struct hpft_pair_state {
    struct bpf_spin_lock lock;
    __u32 epoch;        /* epoch counter; a connection reports once per */
    __u64 epoch_ns;     /* start of the current epoch */
    __u64 sum_cur;      /* sum of trends reported in this epoch */
    __u64 sum_prev;     /* sum of trends of the previous epoch: the divisor */
    __u32 n_cur;        /* connections reported in this epoch */
    __u32 n_prev;       /* connections in the previous epoch */
    __u64 generation;   /* cfg generation last seen (diag) */
    __u32 shots;        /* packets dropped at a connection's debt cap (diag) */
    __u32 nosock;       /* packets that carried no tcp_sock (diag) */
    __u64 reserved;
};

/* per connection (5-tuple): its own clock and its own trend */
struct hpft_flow_state {
    __u64 last_send_ns;
    __u64 next_ns;      /* this connection's earliest-departure clock */
    __u64 win;          /* last window read: snd_cwnd * mss */
    __u64 win_ep;       /* the window this connection reported this epoch */
    __u32 epoch_seen;   /* pair epoch this connection last reported in */
    __u32 pad0;
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
    __u64 now, wire, win = 0, num, sum, R, r_i;
    __u64 send_ns, next_ns, burst_ns, min_next_ns, packet_ns;
    __u32 n_prev, have_tp = 0;

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
        fs_new.epoch_seen = 0xffffffffU;
        fs_new.pad0 = 0;
        bpf_map_update_elem(&hpft_flow_state_map, &flow_key, &fs_new, BPF_ANY);
        fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
        if (!fs)
            return TC_ACT_OK;
    }
    fs->last_send_ns = now;
    if (have_tp)
        fs->win = win;
    else
        win = fs->win;

    /* Step 2: the flow set's denominator, and this connection's share of
     * R. Under the pair's lock: the epoch roll and the per-epoch report. */
    bpf_spin_lock(&state->lock);
    if (now - state->epoch_ns >= HPFT_EPOCH_NS) {
        if (now - state->epoch_ns >= 2 * HPFT_EPOCH_NS) {
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
        state->epoch_ns = now;
    }
    if (!have_tp)
        state->nosock++;
    state->generation = cfg->generation;
    if (have_tp && fs->epoch_seen != state->epoch) {
        /* once per epoch: report this connection's window into the sum */
        __u64 t = win ? win : 1;

        fs->win_ep = t;
        fs->epoch_seen = state->epoch;
        state->sum_cur += t;
        state->n_cur++;
    }
    sum = state->sum_prev;
    n_prev = state->n_prev;
    bpf_spin_unlock(&state->lock);

    R = cfg->rate_bps;
    num = fs->win_ep;                        /* w_i as reported this epoch */
    if (!have_tp || !sum || !num) {
        /* no denominator yet (first epoch of the set), or no window to
         * read: an equal share over the connections seen last epoch */
        __u32 n = n_prev ? n_prev : 1;

        r_i = R / n;
    } else {
        /* R * num / sum without overflow: windows are < 2^32, so scale
         * the ratio to 2^20 first */
        __u64 ratio = (num << 20) / sum;

        if (ratio > (1ULL << 20))
            ratio = 1ULL << 20;
        r_i = (R >> 10) * ratio >> 10;
    }
    if (r_i > R)
        r_i = R;
    if (!r_i)
        r_i = 1;

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
    return TC_ACT_OK;
}

char _license[] SEC("license") = "GPL";
