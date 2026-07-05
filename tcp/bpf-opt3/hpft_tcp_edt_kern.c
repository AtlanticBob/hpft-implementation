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
#define HPFT_PAIR_HORIZON_NS 4000000ULL  /* opt3: drop when aggregate debt runs >4ms ahead */

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

/* opt3: per-flow EDT debt (paces each flow to the pair rate independently);
 * the pair state is reused unchanged as the aggregate debt for cap accounting. */
struct hpft_flow_state {
    struct bpf_spin_lock lock;
    __u32 reserved0;
    __u64 next_ns;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
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
        __u64 flow_key, flow_send_ns, pair_send_ns;
        struct hpft_flow_state *fs;
        int over = 0;

        if ((void *)(tcph + 1) > data_end)
            return TC_ACT_OK;

        now = bpf_ktime_get_ns();
        packet_ns = hpft_bytes_to_ns((__u64)skb->len, cfg->rate_bps);
        burst_ns = hpft_bytes_to_ns((__u64)cfg->burst_bytes, cfg->rate_bps);
        min_next_ns = now > burst_ns ? now - burst_ns : 0;

        flow_key = ((__u64)(iph->saddr ^ iph->daddr) << 32) |
                   ((__u64)tcph->source << 16) | (__u64)tcph->dest;
        flow_key ^= pair_key;

        fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
        if (!fs) {
            struct hpft_flow_state init = {};

            init.next_ns = now;
            bpf_map_update_elem(&hpft_flow_state_map, &flow_key, &init, BPF_ANY);
            fs = bpf_map_lookup_elem(&hpft_flow_state_map, &flow_key);
            if (!fs)
                return TC_ACT_OK;
        }

        /* per-flow EDT debt: paces THIS flow to the pair rate, so a sparse
         * latency flow (any packet size) sees send ~= now even when a bulk
         * flow is saturating the pair. */
        bpf_spin_lock(&fs->lock);
        if (fs->next_ns < min_next_ns)
            fs->next_ns = min_next_ns;
        flow_send_ns = fs->next_ns;
        next_ns = flow_send_ns + packet_ns;
        if (next_ns < flow_send_ns)
            next_ns = flow_send_ns;
        fs->next_ns = next_ns;
        bpf_spin_unlock(&fs->lock);

        /* pair aggregate debt: accounts all flows to enforce the cap. It is
         * NOT used to delay packets (that is per-flow); when the aggregate
         * backlog exceeds the horizon the packet is dropped so TCP backs off,
         * keeping the aggregate at cap without coupling the flows' latency. */
        bpf_spin_lock(&state->lock);
        if (state->generation != cfg->generation) {
            state->next_ns = now;
            state->generation = cfg->generation;
        } else if (state->next_ns < min_next_ns) {
            state->next_ns = min_next_ns;
        }
        pair_send_ns = state->next_ns;
        next_ns = pair_send_ns + packet_ns;
        if (next_ns < pair_send_ns)
            next_ns = pair_send_ns;
        state->next_ns = next_ns;
        bpf_spin_unlock(&state->lock);

        if (pair_send_ns > now + HPFT_PAIR_HORIZON_NS)
            over = 1;

        if (over)
            return TC_ACT_SHOT;
        if (flow_send_ns > now)
            skb->tstamp = flow_send_ns;
        return TC_ACT_OK;
    }
}

char _license[] SEC("license") = "GPL";
