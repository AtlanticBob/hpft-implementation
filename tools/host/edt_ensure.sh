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
# Requires the EDT program already loaded and pinned by the apply tool
# (tools/tcp_shaper/tools/tcp-shaper-apply); this script only repairs attachment drift.
# pair_state seeding lives in hpft_pace_shim.py startup (restart the shim
# after a full EDT re-apply). Idempotent; run on the sender host as root.
set -u
PIN=/sys/fs/bpf/hpft_tcp_edt

if ! sudo test -e "$PIN/hpft_tcp_edt"; then
    echo "EDT program pin missing at $PIN - run tcp-shaper-apply first"
    exit 1
fi

for d in $(ls /sys/class/net | grep -E '^dpu1vf[0-9]+$'); do
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
    # bpftool: hosts do not all run the same kernel, and the one on PATH is
    # not always new enough to read a pinned program. An empty `want` makes
    # the comparison below unfalsifiable, which is how a check meant to catch
    # a stale program starts re-attaching on every run instead.
    B=${HPFT_BPFTOOL:-$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1)}
    B=${B:-$(command -v bpftool)}
    want=$(sudo "$B" prog show pinned "$PIN/hpft_tcp_edt" 2>/dev/null \
           | head -1 | cut -d: -f1)
    [ -n "$want" ] || echo "  WARN $d: cannot read the pinned program id ($B)"
    # Every program id attached, not just the first one tc prints: `tc filter
    # replace` without a handle ADDS a filter, so a re-apply used to leave the
    # previous program (with its now-unpinned maps and stale rates) attached
    # underneath the new one. Anything other than exactly {want} is torn down.
    have=$(tc filter show dev "$d" egress 2>/dev/null \
           | grep -o 'id [0-9]*' | cut -d' ' -f2 | sort -u | tr '\n' ' ')
    if [ -z "$want" ] || [ "$have" != "$want " ]; then
        sudo tc filter del dev "$d" egress pref 10 2>/dev/null
        sudo tc filter add dev "$d" egress pref 10 handle 1 protocol all \
               bpf da object-pinned "$PIN/hpft_tcp_edt"
    fi
    echo "$d: fq=$(tc qdisc show dev "$d" | grep -c '^qdisc fq') edt_prog=$(tc filter show dev "$d" egress | grep -o 'id [0-9]*' | cut -d' ' -f2 | sort -u | tr '\n' ',') want=$want"
done
