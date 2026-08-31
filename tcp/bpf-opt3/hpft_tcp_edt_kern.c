// SPDX-License-Identifier: GPL-2.0
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/if_vlan.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/pkt_cls.h>
#include <linux/tcp.h>

#include "hpft_bpf_helpers.h"

#define HPFT_MAX_VNICS 4096
#define HPFT_MAX_PAIRS 16384
#define HPFT_NSEC_PER_SEC 1000000000ULL
#define HPFT_GAP_NS 40000ULL  /* inter-pkt gap > 40us: flow idled (design §5.3) */
/* A bypass candidate must also be a SMALL packet. The VF MTU is 1500, so a
 * non-TSO frame is <= ~1514 B at this hook; anything larger is a TSO
 * super-packet, i.e. bulk traffic, which must never skip the shaper. */
#define HPFT_SPARSE_MAX_LEN 2048ULL
/* Per-wire-frame bytes the shaper must charge for but skb->len never
 * counts: 8 B preamble+SFD, 12 B inter-packet gap, 4 B FCS. */
#define HPFT_FRAME_OVERHEAD 24ULL
/* Max shaping debt a pair may carry, as future-stamp distance. Beyond it,
 * packets are dropped (policer tail on the EDT shaper). Enforcement itself
 * comes from the stamps + a STABLE generation; this drop is only the
 * backstop against unbounded debt (a CC that never yields). Sized 200 ms:
 * at 50 ms a flow-set carrying ~128 connections built past the cap on
 * normal bursts and the resulting loss collapsed TCP to a tenth of its
 * share (2-3-1 S2 heavy rung, 2026-07-30). fq's own horizon is 10 s, so
 * 200 ms of queue is cheap; runaway debt still trips within a few ticks. */
#define HPFT_DEBT_CAP_NS (200ULL * 1000 * 1000)

/* ---- reading the tenant's CC instead of editing it --------------------
 * Same three steps as the RDMA executor, on the same terms: read what the
 * connection's congestion control has arrived at, combine that with the
 * policy share, shape the wire to the result. The kernel CC is never
 * touched - it is not even aware of this - and only its DECREASES are
 * read, because a CC's absolute rate is calibrated against the link and
 * says nothing about a share it has never been told about.
 *
 *      rate = d * share,   d in [f, 1]
 *      d <- d * (cc / cc_prev)   when the CC lowered its window
 *      d <- (1 + d) / 2          otherwise, once per recovery period
 *
 * What is sampled here is snd_cwnd * mss summed over the flow-set's
 * connections. cwnd is the CC's own decision variable, so a cut in it is
 * the CC acting rather than the network being noisy; srtt deliberately
 * does not enter, since it is a measurement and would put its jitter into
 * every sample. The window is not a rate, but only ratios are read and
 * the RTT that would convert one to the other cancels out of a ratio
 * taken across a cut.
 *
 * ATTRIBUTION. The shaper's own queue is a congestion signal to the CC
 * above it - that is what the debt cap's drops are for - so counting a cut
 * we caused would let shaping drive more shaping. A cut is counted only
 * when the shaper is NOT holding the flow-set back, i.e. when the EDT
 * stamp is not already in the future. At a bottleneck the allocator owns,
 * the share is the answer to the queue and the CC's opinion of it is not
 * needed; where something the allocator cannot see is the limit, the
 * flow-set cannot reach its stamp, and the CC is heard in full. */
#define HPFT_D_ONE 1048576ULL       /* 1.0, matching the RDMA executor's fxp20 */
#define HPFT_D_FLOOR (HPFT_D_ONE >> 6)
#define HPFT_D_RECOVER_NS 300000ULL /* one half-gap step, as on the DPA */
/* fence design (2026-08-26): cfg->flags bit31 set => trust mode, low 16
 * bits = trust T in fxp16. The pair is shaped to clip(cc, (1-T)*rate,
 * rate) where cc is the flow-set's aggregate cwnd*mss/rtt_min (6.1). */
#define HPFT_TRUST_FLAG 0x80000000U
#define HPFT_LINE_BPS 200000000000ULL
#define HPFT_TRUST_EPOCH_NS 1000000ULL      /* trust updated once per ms */
#define HPFT_TRUST_STEP 66ULL               /* fxp16 per ms = 1 ms / 1 s */
/* design v4 6.3: how far back a retransmission still counts as "the
 * network is dropping this flow-set right now" - the observation window. */
#define HPFT_LOSS_WIN_NS 100000000ULL
/* How long a drop of our own keeps the fast path shut, in ms. It has to
 * outlast the retransmission that drop will provoke plus the window that
 * retransmission would then sit in, so any loss the shaper could have
 * caused is out of the picture before loss is read as the network's. */
#define HPFT_SHOT_QUIET_MS 300U
/* "no baseline yet" for a flow's retransmission counter: an entry created
 * on a packet that carried no socket has nothing to compare against, and
 * comparing against zero would read the connection's whole history as one
 * fresh loss. */
#define HPFT_RETR_UNKNOWN 0xffffffffU

struct hpft_vlan_hdr {
    __u16 h_vlan_TCI;
    __u16 h_vlan_encapsulated_proto;
};

struct hpft_rate_cfg {
    __u64 rate_bps;
    __u64 generation;
    __u32 burst_bytes;
    __u32 flags;
};

struct hpft_pair_state {
    struct bpf_spin_lock lock;
    /* ms (monotonic) of the last packet this shaper dropped at its debt
     * cap. The loss fast path treats a retransmission as the NETWORK's
     * statement of loss, which it only is if the loss was not ours: the
     * design's premise is a shaper that never drops, and the debt cap is
     * the one place this one does. */
    __u32 shot_ms;
    __u64 next_ns;
    __u64 generation;
    /* observer: cc_sum is this flow-set's aggregate CC window, maintained
     * incrementally as each connection reports its own; cc_prev is the last
     * aggregate acted on. */
    __u64 cc_sum;
    __u64 cc_prev;
    __u64 d_ts;
    __u32 d;
    /* cuts the observer actually counted. d recovers in about a
     * millisecond, so point-sampling d almost always reads 1.0 even while
     * the active branch is working; this counter is the evidence that it
     * is. */
    __u32 cuts;
    /* fence design: executor-owned trust (fxp16) and its last update time */
    __u32 trust;
    /* epochs in which loss evidence raised the trust (6.3). Trust alone
     * is a level; this counts the events that moved it. */
    __u32 loss_ep;
    __u64 trust_ns;
    /* when this flow-set last retransmitted anything (design v4 6.3).
     * Zero means it never has. */
    __u64 loss_ns;
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

/* opt3: per-flow last-send timestamp for inter-packet-gap detection. A flow
 * that idles between packets (request/response latency flow, any packet size)
 * bypasses the shared pair pacing; a continuous bulk flow does not. */
struct hpft_flow_state {
    __u64 last_send_ns;
    __u64 cc_win;      /* this connection's last cwnd*mss, in the pair sum */
    __u32 retrans;     /* this connection's last total_retrans, for the delta */
    __u32 pad0;
};

struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);   /* LRU: dead flows age out automatically, no lock needed */
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
    struct iphdr *iph;
    __u32 *src_vnic;
    __u32 *dst_vnic;
    __u32 src_idx = 0;
    __u32 ifindex;
    __u64 pair_key;
    __u64 now;
    __u64 send_ns;
    __u64 next_ns;
    __u64 burst_ns;
    __u64 min_next_ns;
    __u64 packet_ns;

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
    if (!cfg || !state || !cfg->rate_bps)
        return TC_ACT_OK;

    {
        void *data_end = (void *)(long)skb->data_end;
        struct tcphdr *tcph = (void *)iph + (iph->ihl * 4);
        __u64 flow_key, gap = HPFT_GAP_NS + 1;
        struct hpft_flow_state *fs;
        struct hpft_flow_state fs_new;
        __u64 eff_rate, wire = 0;
        __s64 cc_delta = 0;
        __u32 d;

        if ((void *)(tcph + 1) > data_end)
            return TC_ACT_OK;

        now = bpf_ktime_get_ns();
        /* Meter WIRE bytes, not skb bytes. The policy share is defined
         * against link capacity and the link carries framing, but skb->len
         * counts the L2/L3/L4 headers of a GSO super-packet ONCE and knows
         * nothing about preamble/SFD/IPG/FCS. Measured 2026-07-27: a pair
         * paced at 10G delivered 9.79G of goodput and 10.6G on the wire,
         * so every TCP flow-set was quietly consuming ~8% more link than
         * its share - a systematic bias in favour of TCP over RDMA, whose
         * budget the RP enforces against the wire.
         * Each wire frame adds its own L2+L3+L4 headers (the super-packet
         * carried one copy) plus 8 B preamble+SFD, 12 B inter-packet gap
         * and 4 B FCS. */
        {
            __u32 segs = skb->gso_segs ? skb->gso_segs : 1;
            __u32 hdr = ETH_HLEN + (__u32)iph->ihl * 4 +
                        (__u32)tcph->doff * 4;

            wire = (__u64)skb->len +
                   (__u64)(segs - 1) * (__u64)hdr +
                   (__u64)segs * HPFT_FRAME_OVERHEAD;
        }

        /* inter-packet gap per flow (5-tuple). A flow seen for the first
         * time is not entered here but after the socket has been read:
         * its window and its retransmission count are the baselines the
         * next packet subtracts from, and seeding either with zero makes
         * the next packet read the connection's whole history as one
         * step. (Seeding cc_win with zero also added this connection's
         * window to the pair's aggregate twice - once as the new flow's
         * whole window, once as the first delta against zero.) */
        flow_key = ((__u64)(iph->saddr ^ iph->daddr) << 32) |
                   ((__u64)tcph->source << 16) | (__u64)tcph->dest;
        flow_key ^= pair_key;
        fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
        if (fs) {
            gap = now - fs->last_send_ns;
            fs->last_send_ns = now;
        }

        /* Step 1: read the CC. cwnd*mss is this connection's contribution to
         * the flow-set's aggregate window; cc_delta is what it changed by,
         * which is what the pair's running sum needs. total_retrans is the
         * stack's own count of what it had to send again, whichever CC is
         * driving it - the fast path of 6.3 step three reads its delta. */
        __u64 srtt_us = 0;
        __u64 rtt_min_us = 0;
        __u32 saw_loss = 0;
        __u32 have_tp = 0;
        __u64 win = 0;
        __u32 retr = 0;
        {
            struct bpf_sock *sk = skb->sk;

            if (sk) {
                struct bpf_sock *full = bpf_sk_fullsock(sk);

                if (full) {
                    struct bpf_tcp_sock *tp = bpf_tcp_sock(full);

                    if (tp) {
                        win = (__u64)tp->snd_cwnd * (__u64)tp->mss_cache;
                        retr = tp->total_retrans;
                        have_tp = 1;

                        srtt_us = (__u64)tp->srtt_us >> 3;
                        rtt_min_us = (__u64)tp->rtt_min;
                        if (fs) {
                            cc_delta = (__s64)win - (__s64)fs->cc_win;
                            fs->cc_win = win;
                            if (fs->retrans != HPFT_RETR_UNKNOWN &&
                                retr > fs->retrans)
                                saw_loss = 1;
                            fs->retrans = retr;
                        } else {
                            cc_delta = (__s64)win;
                        }
                    }
                }
            }
        }
        if (!fs) {
            fs_new.last_send_ns = now;
            fs_new.cc_win = win;
            fs_new.retrans = have_tp ? retr : HPFT_RETR_UNKNOWN;
            fs_new.pad0 = 0;
            bpf_map_update_elem(&hpft_flow_state_map, &flow_key, &fs_new, BPF_ANY);
        }

        /* shared pair debt: always accumulate (cap + fairness + smooth pacing,
         * identical to baseline; keeps multi-flow stable). */
        bpf_spin_lock(&state->lock);
        if (state->d == 0) {
            state->d = (__u32)HPFT_D_ONE;
            state->d_ts = now;
        }
        if (state->generation != cfg->generation) {
            /* Reconfig: forgive only STALE debt (cap the stamp horizon),
             * never zero it. Resetting to `now` on every generation bump
             * amnestied the whole debt each shim push (~100 ms cadence
             * under an oscillating target) and let a deep-debt sender run
             * 1.6x its pace -- the BBR escape (2-1-2 expM). */
            if (state->next_ns > now + HPFT_DEBT_CAP_NS)
                state->next_ns = now + HPFT_DEBT_CAP_NS;
            state->generation = cfg->generation;
        }
        if (saw_loss)
            state->loss_ns = now;
        /* Step 2: combine the CC's reading with the policy share. A stamp
         * already in the future means the shaper is the reason this
         * flow-set is not sending faster, so a cut taken now is one we
         * caused and is not counted against the share. */
        {
            __u64 sum = state->cc_sum;
            __u64 prev = state->cc_prev;
            __u64 nsum;
            int pace_limited = state->next_ns > now;

            if (cc_delta >= 0) {
                nsum = sum + (__u64)cc_delta;
            } else {
                __u64 drop = (__u64)(-cc_delta);

                nsum = sum > drop ? sum - drop : 0;
            }
            state->cc_sum = nsum;
            if (nsum < prev) {
                if (!pace_limited && prev) {
                    __u64 nd = ((__u64)state->d * nsum) / prev;

                    state->d = nd < HPFT_D_FLOOR ? (__u32)HPFT_D_FLOOR
                                                 : (__u32)nd;
                    state->cuts++;
                }
                state->cc_prev = nsum;
            } else if (nsum > prev) {
                state->cc_prev = nsum;
            }
            if (now - state->d_ts >= HPFT_D_RECOVER_NS) {
                state->d_ts = now;
                if (state->d < HPFT_D_ONE)
                    state->d = (__u32)((HPFT_D_ONE >> 1) + (state->d >> 1));
            }
            d = state->d;
        }
        /* Step 3: shape to it. */
        if (cfg->flags & HPFT_TRUST_FLAG) {
            /* design v4 6.1: r = clip(cc, (1-T)*rate, rate). The fence
             * is the upper bound at every trust value - trust buys the
             * right to send LESS, never more - and the lower bound opens
             * linearly with trust. Inside the band the wire carries
             * exactly what the CC asked for, so a CC that is behaving
             * sees an unmodified network and its own loop is intact;
             * r = T*cc + (1-T)*rate never has that property, and its
             * upper bound (1-T)*rate + T*line let 12% of trust leak 24 G
             * per flow-set at line rate (measured).
             *
             * cc = the CC's allowance as a rate: aggregate window over
             * the connection's MINIMUM rtt (what it would send with
             * nothing queued; over srtt it merely restates the paced
             * rate), capped at line rate. The flags' low 16 bits carry
             * the queue fraction q/D_r.
             *
             * Trust, once per ms: decayed by the queue fraction, raised
             * only on evidence of a bottleneck the receiver's ledger
             * cannot see (6.3). That evidence is loss: our shaper only
             * delays, so a retransmission is the network's own statement
             * that it dropped this flow-set. Asking instead whether the
             * CC is what limits the flow - the old cwnd-limited test -
             * cannot tell a CC that yields too early at a bottleneck we
             * DO manage from one facing a bottleneck we do not, and in
             * the first case it raises trust exactly when the flow is
             * giving its share away.
             *
             * "Not us" has one exception here: the debt cap below drops.
             * A drop of ours provokes a retransmission of ours, so the
             * evidence is ignored for HPFT_SHOT_QUIET_MS after the
             * shaper last dropped anything for this flow-set. The RDMA
             * executor needs no such clause - it only makes a QP wait. */
            __u64 qf = cfg->flags & 0xffffULL;
            __u64 cc_bps = 0;
            __u64 rtt_ref = rtt_min_us ? rtt_min_us : srtt_us;
            __u64 t, lo;

            if (rtt_ref) {
                cc_bps = (state->cc_sum * 8000000ULL) / rtt_ref;
                if (cc_bps > HPFT_LINE_BPS)
                    cc_bps = HPFT_LINE_BPS;
            }
            if (now - state->trust_ns >= HPFT_TRUST_EPOCH_NS) {
                __u32 now_ms = (__u32)(now / 1000000ULL);
                int ours = state->shot_ms &&
                           now_ms - state->shot_ms < HPFT_SHOT_QUIET_MS;
                int lost = !ours && state->loss_ns &&
                           now - state->loss_ns < HPFT_LOSS_WIN_NS &&
                           cc_bps && cc_bps < cfg->rate_bps;

                state->trust_ns = now;
                t = state->trust;
                t -= (t * qf) >> 16;
                if (qf == 0 && lost) {
                    t += ((65536ULL - t) * HPFT_TRUST_STEP) >> 16;
                    state->loss_ep++;
                }
                state->trust = t > 65536ULL ? 65536U : (__u32)t;
            }
            t = state->trust;
            lo = (cfg->rate_bps * (65536ULL - t)) >> 16;
            eff_rate = cc_bps;
            if (eff_rate > cfg->rate_bps)
                eff_rate = cfg->rate_bps;
            if (eff_rate < lo)
                eff_rate = lo;
        } else {
            eff_rate = (cfg->rate_bps * (__u64)d) >> 20;
        }
        if (!eff_rate)
            eff_rate = 1;
        packet_ns = hpft_bytes_to_ns(wire, eff_rate);
        burst_ns = hpft_bytes_to_ns((__u64)cfg->burst_bytes, eff_rate);
        min_next_ns = now > burst_ns ? now - burst_ns : 0;
        if (state->next_ns < min_next_ns)
            state->next_ns = min_next_ns;
        send_ns = state->next_ns;
        if (send_ns > now + HPFT_DEBT_CAP_NS) {
            /* Debt beyond the cap: drop instead of stamping ever further
             * into the future. Bounds fq queueing delay to the cap and
             * hands rate-based CCs (BBR) a real loss signal; loss-based
             * CCs never dig this deep. State is NOT advanced. */
            state->shot_ms = (__u32)(now / 1000000ULL);
            bpf_spin_unlock(&state->lock);
            return TC_ACT_SHOT;
        }
        next_ns = send_ns + packet_ns;
        if (next_ns < send_ns)
            next_ns = send_ns;
        state->next_ns = next_ns;
        bpf_spin_unlock(&state->lock);

        /* Latency-sparse bypass (design.md §5.3): a request/response flow
         * that idles between packets should not queue behind a bulk flow's
         * shaping debt. THREE conditions, all necessary - the original
         * single gap test did not enforce the cap at all in the common
         * case (2026-07-27 incast8, measured):
         *
         *  - gap > HPFT_GAP_NS: the flow idled. The original intent.
         *  - skb->len small: a TSO super-packet is never a latency
         *    sensitive request. This is the condition whose absence broke
         *    the cap. With TSO on, a pair's rate divided among several
         *    bulk streams gives EVERY stream an inter-packet gap above the
         *    threshold (4 iperf3 streams sharing 14.85G => one 64 KB skb
         *    per stream per ~141 us), so every packet took the bypass,
         *    nothing was ever delayed, and the pair ran 1.13-1.46x its
         *    pace for 85 s while the debt grew unread. Whether a run
         *    enforced or escaped depended on how bursty TCP happened to
         *    be, which is why identical reps measured 94% and 146%.
         *  - the pair is within one burst of its debt: past that the cap
         *    outranks the latency favour. Without it, many sparse small
         *    flows could still walk through the cap in aggregate.
         *
         * The comment this replaces claimed the cap stayed accurate
         * because bypassed bytes still charge the debt. They do - but a
         * debt nobody ever waits on enforces nothing.
         */
        if (send_ns > now) {
            int sparse = gap > HPFT_GAP_NS &&
                         (__u64)skb->len <= HPFT_SPARSE_MAX_LEN &&
                         (send_ns - now) <= burst_ns;
            /* max(): the sock may carry its own EDT stamp (BBR's pacing
             * writes skb->tstamp directly). Composing by max() in the
             * time domain is min() in the rate domain -- the actual send
             * rate is min(CC's own pacing, HPFT budget). Overwriting
             * unconditionally discarded whichever stamp was later and
             * let BBR run ~22% past its budget (2-1-2 expM finding). */
            if (!sparse && send_ns > skb->tstamp)
                skb->tstamp = send_ns;
        }
        return TC_ACT_OK;
    }
}

char _license[] SEC("license") = "GPL";
