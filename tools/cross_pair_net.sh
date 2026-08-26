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
# NOT ENOUGH ON ITS OWN FOR rdma_cm (2026-08-23). With perftest's -R the
# kernel resolves the route from the DESTINATION first and picks the VF that
# owns that subnet, whatever -d says, and these rules never see a source
# address to match on. The flow is then a STRAIGHT pair: the receiver
# attributes it to the wrong sender VF, the RP tags it as the destination's
# own pair, and the experiment measures a topology nobody asked for while
# reporting normally. Cross-pair traffic must use the NON-CM path -- e.g.
# `ib_write_bw -d <src dev> -x 3 ... <dst ip>` -- which binds the source GID
# to the device and produces the pair the runner intended. Verified by the
# receiver's own MAC classification: -R gave vf1>vf1, -x 3 gave vf0>vf1.
#
# Usage: bash cross_pair_net.sh apply|revert|status
# Run on each host. Not persistent across reboot - reapply after host reboot.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation

# The host's last octet is its VF addressing, and that is in the registry;
# with four machines a case list here would just be a second copy of it that
# nobody remembers to extend.
OCT=$(python3 -c "
import json, socket, sys
r = json.load(open('$REPO/config/lab-registry.json'))
me = socket.gethostname()
ips = [v['ip'] for v in r['vnics'] if v['host'] == me]
if not ips: sys.exit('cross_pair_net: %s has no vnics in the registry' % me)
oct4 = {ip.split('.')[-1] for ip in ips}
if len(oct4) != 1: sys.exit('cross_pair_net: %s vnics do not share a last octet' % me)
print(oct4.pop())") || exit 1

NVF=$(python3 -c "
import json, socket
r = json.load(open('$REPO/config/lab-registry.json'))
print(len([v for v in r['vnics'] if v['host'] == socket.gethostname()]))")
cmd=${1:-status}
for i in $(seq 0 $((NVF-1))); do
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
        # match by TABLE, not by the string "lookup <n>": a table with a
        # name in /etc/iproute2/rt_tables prints the name, and the check
        # then reports a rule that is installed and working as absent.
        ip rule show table "$tbl" 2>/dev/null | grep -q "from $src" && st=on || st=off
        echo "$dev $src table=$tbl rule=$st" \
             "arp_ignore=$(sysctl -n net.ipv4.conf.$dev.arp_ignore)" \
             "rp_filter=$(sysctl -n net.ipv4.conf.$dev.rp_filter)"
        ;;
    esac
done
[ "$cmd" = apply ] && bash "$0" status
exit 0
