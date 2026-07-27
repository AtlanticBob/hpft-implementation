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
    __u32 reserved0;
    __u64 next_ns;
    __u64 generation;
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

        if ((void *)(tcph + 1) > data_end)
            return TC_ACT_OK;

        now = bpf_ktime_get_ns();
        packet_ns = hpft_bytes_to_ns((__u64)skb->len, cfg->rate_bps);
        burst_ns = hpft_bytes_to_ns((__u64)cfg->burst_bytes, cfg->rate_bps);
        min_next_ns = now > burst_ns ? now - burst_ns : 0;

        /* inter-packet gap per flow (5-tuple) */
        flow_key = ((__u64)(iph->saddr ^ iph->daddr) << 32) |
                   ((__u64)tcph->source << 16) | (__u64)tcph->dest;
        flow_key ^= pair_key;
        fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
        if (fs) {
            gap = now - fs->last_send_ns;
            fs->last_send_ns = now;
        } else {
            fs_new.last_send_ns = now;
            bpf_map_update_elem(&hpft_flow_state_map, &flow_key, &fs_new, BPF_ANY);
        }

        /* shared pair debt: always accumulate (cap + fairness + smooth pacing,
         * identical to baseline; keeps multi-flow stable). */
        bpf_spin_lock(&state->lock);
        if (state->generation != cfg->generation) {
            state->next_ns = now;
            state->generation = cfg->generation;
        } else if (state->next_ns < min_next_ns) {
            state->next_ns = min_next_ns;
        }
        send_ns = state->next_ns;
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
            if (!sparse)
                skb->tstamp = send_ns;
        }
        return TC_ACT_OK;
    }
}

char _license[] SEC("license") = "GPL";
