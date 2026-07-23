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
# (hpft-exp-deprecated tcp-shaper-apply); this script only repairs attachment drift.
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
    tc filter show dev "$d" egress | grep -q hpft_tcp_edt \
        || sudo tc filter add dev "$d" egress bpf da object-pinned "$PIN/hpft_tcp_edt"
    echo "$d: fq=$(tc qdisc show dev "$d" | grep -c '^qdisc fq') edt=$(tc filter show dev "$d" egress | grep -c hpft_tcp_edt)"
done
