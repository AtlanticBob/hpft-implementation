#!/usr/bin/env bash
# Ensure the host fq+edt TCP actuator covers every local VF netdev.
#
# Why: stress D1 (2026-07-11) found the EDT tc filter attached on dpu1vf0
# only and root fq missing on vf1-3, so TCP sourced from vf1-3 bypassed
# pacing entirely (28-fs contention blew each dst VM's 20G MaxRate to
# 46-55G). The tc layer is runtime state - it is lost when a VF netdev is
# recreated (DPU reboot) - so coverage must be re-ensured, same as the
# rx_agent's OpenFlow classification rules on the receiver DPU.
#
# Requires the EDT program already loaded and pinned by the v1 apply tool
# (tools/tcp_shaper/tools/tcp-shaper-apply); this script only repairs attachment drift.
# pair_state seeding lives in hpft_pace_shim.py startup (restart the shim
# after a full EDT re-apply). Idempotent; run on the sender host as root.
set -u
PIN=/sys/fs/bpf/hpft_tcp_edt

if ! sudo test -e "$PIN/hpft_tcp_edt"; then
    echo "EDT program pin missing at $PIN - run the v1 tcp-shaper-apply first"
    exit 1
fi

for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do
    ip link show "$d" > /dev/null 2>&1 || { echo "$d: absent, skipped"; continue; }
    tc qdisc show dev "$d" | grep -q "^qdisc fq [0-9a-f]*: root" \
        || sudo tc qdisc replace dev "$d" root fq
    sudo tc qdisc add dev "$d" clsact 2>/dev/null
    # Compare the attached program's ID against the CURRENTLY PINNED one,
    # not just "is something called hpft_tcp_edt attached". A reload leaves
    # the previous program alive as long as a filter still references it,
    # and the old filter matches by name - so the name test silently left
    # three of four VFs running the PREVIOUS program with its PREVIOUS
    # maps. Measured 2026-07-27: vf1/vf2/vf3 kept enforcing the static
    # rdma_rules rates (4G/2G/2G) from the old cfg map while the new map
    # showed the correct 12G, and a single TCP pair on an idle 100G link
    # sat at exactly 2.00 Gb/s. Cost me most of an incast investigation.
    want=$(sudo bpftool prog show pinned "$PIN/hpft_tcp_edt" 2>/dev/null \
           | head -1 | cut -d: -f1)
    have=$(tc filter show dev "$d" egress 2>/dev/null \
           | grep -o 'id [0-9]*' | head -1 | cut -d' ' -f2)
    [ -n "$want" ] && [ "$have" = "$want" ] \
        || sudo tc filter replace dev "$d" egress pref 10 protocol all \
               bpf da object-pinned "$PIN/hpft_tcp_edt"
    echo "$d: fq=$(tc qdisc show dev "$d" | grep -c '^qdisc fq') edt_prog=$(tc filter show dev "$d" egress | grep -o 'id [0-9]*' | head -1 | cut -d' ' -f2) want=$want"
done
