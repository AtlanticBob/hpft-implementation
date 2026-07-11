#!/usr/bin/env bash
# Cross-VF-pair routing for the 4x4 flow-set mesh (scale/stress experiments).
#
# Why: each VF pair lives in its own /24 (10.1.i.x). A flow from src VF i to
# dst VF j (i != j) would otherwise egress via the connected route of VF j's
# subnet - i.e. the WRONG source VF - and be misattributed by MAC on the
# receiver DPU. Per-source policy routing pins egress to the VF that owns the
# source IP. arp_ignore=1 stops sibling VFs (same L2 through the switch) from
# answering ARP for each other's IPs; rp_filter=2 (loose) accepts ingress from
# foreign subnets.
#
# Usage: bash cross_pair_net.sh apply|revert|status
# Run on each host (sgpu01: last octet .1, sgpu02: last octet .2). Not
# persistent across reboot - reapply after host reboot.
set -u

case "$(hostname)" in
    sgpu01) OCT=1 ;;
    sgpu02) OCT=2 ;;
    *) echo "unknown host $(hostname)"; exit 1 ;;
esac

cmd=${1:-status}
for i in 0 1 2 3; do
    dev="dpu1vf$i"
    src="10.1.$i.$OCT"
    tbl=$((100 + i))
    case "$cmd" in
    apply)
        sudo ip rule del from "$src" lookup "$tbl" 2>/dev/null
        sudo ip rule add from "$src" lookup "$tbl" pref "$tbl"
        sudo ip route replace 10.1.0.0/16 dev "$dev" src "$src" table "$tbl"
        sudo sysctl -qw "net.ipv4.conf.$dev.arp_ignore=1" \
                        "net.ipv4.conf.$dev.rp_filter=2"
        ;;
    revert)
        sudo ip rule del from "$src" lookup "$tbl" 2>/dev/null
        sudo ip route flush table "$tbl" 2>/dev/null
        ;;
    status)
        ip rule show | grep -q "from $src lookup $tbl" && st=on || st=off
        echo "$dev $src table=$tbl rule=$st" \
             "arp_ignore=$(sysctl -n net.ipv4.conf.$dev.arp_ignore)" \
             "rp_filter=$(sysctl -n net.ipv4.conf.$dev.rp_filter)"
        ;;
    esac
done
[ "$cmd" = apply ] && bash "$0" status
exit 0
