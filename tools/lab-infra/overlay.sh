#!/usr/bin/env bash
# The resident VxLAN overlay, as a STAR centred on the hub.
#
# Generalized overlay (P0 spike, results/p0_overlay_20260729): every VF pair,
# VF IPs untouched, telemetry on p1, tos=inherit (the ECN bit has to survive
# encapsulation or the tenant CC sees a lossless network).
#
# STAR, not mesh (2026-08-23): a full mesh of point-to-point tunnels on one
# OVS bridge has no split horizon. A broadcast entering on one tunnel is
# flooded back out of the others, comes home by a second path, and the bridge
# learns every remote MAC on the wrong port - measured on four nodes as
# instant cross-node mis-learning and total loss of VF connectivity. A star is
# acyclic, gives every sender a direct one-hop tunnel to the receiver, and
# still carries spoke-to-spoke traffic through the hub's eswitch at line rate.
#
# The hub therefore wants to be the receiver. It defaults to the registry's.
#
# usage: overlay.sh [--hub HOST] [--teardown]
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
cd "$REPO"
HUB=""; MODE=build
while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB=$2; shift 2 ;;
    --teardown) MODE=teardown; shift ;;
    *) echo "overlay.sh: unknown argument $1"; exit 1 ;;
  esac
done
HUB=${HUB:-$(python3 -c "import json;print(json.load(open('config/lab-registry.json'))['receiver_host'])")}
# how many VFs a host carries is a registry fact (8 since 2026-08-26), not a loop bound
NVF=$(python3 -c "import json;r=json.load(open('config/lab-registry.json'));print(max(len([v for v in r['vnics'] if v['host']==n['host']]) for n in r['nodes']))")
VFIDX=$(seq -s ' ' 0 $((NVF-1)))

mapfile -t NODES < <(python3 -c "
import json
for i, n in enumerate(json.load(open('config/lab-registry.json'))['nodes'], 1):
    print(n['host'], n['dpu'], n['underlay_ip'], n['telemetry_ip'], i)")
HUB_IDX=$(for n in "${NODES[@]}"; do set -- $n; [ "$1" = "$HUB" ] && echo "$5"; done)
[ -n "${HUB_IDX:-}" ] || { echo "overlay.sh: $HUB is not a node"; exit 1; }

if [ "$MODE" = teardown ]; then
  # Tear the overlay down and restore the DIRECT data path: delete ovsbr-p1,
  # move the representors back onto underlay-p1, AND put the p1 uplink back
  # into it. The p1 add-back is essential: building the overlay turns p1 into
  # a standalone L3 netdev, so after teardown the bridge has the representors
  # but NO uplink to the wire -- the "cc all correct, data plane totally
  # broken" symptom is exactly p1 missing from the bridge.
  for n in "${NODES[@]}"; do
    set -- $n
    ssh -o BatchMode=yes "$2" 'sudo ovs-vsctl --if-exists del-br ovsbr-p1
      sudo ip addr flush dev p1 2>/dev/null
      sudo ovs-vsctl --may-exist add-port underlay-p1 p1
      for i in $VFIDX; do sudo ovs-vsctl --may-exist add-port underlay-p1 pf1vf$i; done
      sudo ip link set p1 up' &
  done
  wait
  echo "overlay: torn down -> direct data path"
  exit 0
fi

echo "== VxLAN overlay: star, hub=$HUB (idempotent) =="
for n in "${NODES[@]}"; do
  set -- $n
  host=$1; dpu=$2; ul=$3; tel=$4; idx=$5
  (
    # p1 must leave underlay-p1 and be a standalone L3 netdev: the underlay IP
    # sits straight on p1 and the kernel routes encap traffic over it, which
    # an OVS bridge PORT (pure L2) cannot do -- also the documented
    # precondition for VxLAN hardware offload. The telemetry IP rides p1 as a
    # second address.
    ssh -o BatchMode=yes "$dpu" "
      sudo ovs-vsctl --if-exists del-port underlay-p1 p1 2>/dev/null
      sudo ovs-vsctl --if-exists del-port ovsbr2 p1 2>/dev/null
      sudo ip addr add $ul/24 dev p1 2>/dev/null
      sudo ip addr add $tel/24 dev p1 2>/dev/null
      sudo ip link set p1 up mtu 9000
      sudo ovs-vsctl --may-exist add-br ovsbr-p1
      for i in $VFIDX; do
        sudo ovs-vsctl --if-exists del-port underlay-p1 pf1vf\$i 2>/dev/null
        sudo ovs-vsctl --if-exists del-port ovsbr2 pf1vf\$i 2>/dev/null
        sudo ovs-vsctl --may-exist add-port ovsbr-p1 pf1vf\$i
      done
      sudo ip link set ovsbr-p1 up"
  ) &
done
wait

# tunnels: hub gets one per spoke, each spoke exactly one to the hub
for n in "${NODES[@]}"; do
  set -- $n
  host=$1; dpu=$2; ul=$3; idx=$5
  want=""
  if [ "$idx" = "$HUB_IDX" ]; then
    for m in "${NODES[@]}"; do set -- $m; [ "$5" = "$HUB_IDX" ] || want="$want $5:$3"; done
  else
    for m in "${NODES[@]}"; do set -- $m; [ "$5" = "$HUB_IDX" ] && want="$want $5:$3"; done
  fi
  (
    keep=""
    for w in $want; do
      peer=${w%%:*}; pip=${w#*:}
      # the original pair's tunnel keeps its name so anything that greps for
      # vxlan100 (overlay_present, older runners) still recognises the overlay
      name=vx$peer; { [ "$idx" -le 2 ] && [ "$peer" -le 2 ]; } && name=vxlan100
      keep="$keep $name"
      ssh -o BatchMode=yes "$dpu" "sudo ovs-vsctl --may-exist add-port ovsbr-p1 $name -- set interface $name \
        type=vxlan options:local_ip=$ul options:remote_ip=$pip options:key=100 options:dst_port=4789 options:tos=inherit"
    done
    for pt in $(ssh -o BatchMode=yes "$dpu" 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | grep -E "^(vx|vxlan)"'); do
      echo " $keep " | grep -q " $pt " || ssh -o BatchMode=yes "$dpu" "sudo ovs-vsctl --if-exists del-port ovsbr-p1 $pt"
    done
    ssh -o BatchMode=yes "$dpu" 'sudo ovs-appctl fdb/flush ovsbr-p1 >/dev/null 2>&1; true'
  ) &
done
wait

# static telemetry ARP mesh + a clean neighbour table on every host
declare -A MAC
for n in "${NODES[@]}"; do set -- $n; MAC[$2]=$(ssh -o BatchMode=yes "$2" 'cat /sys/class/net/p1/address' 2>/dev/null); done
for a in "${NODES[@]}"; do
  set -- $a; da=$2
  for b in "${NODES[@]}"; do
    set -- $b; db=$2; tb=$4
    [ "$da" = "$db" ] && continue
    [ -n "${MAC[$db]:-}" ] && ssh -o BatchMode=yes "$da" "sudo arp -s $tb ${MAC[$db]} 2>/dev/null" &
  done
done
wait
for n in "${NODES[@]}"; do
  set -- $n
  if [ "$1" = "$(hostname)" ]; then sudo ip -s -s neigh flush all >/dev/null 2>&1
  else ssh -o BatchMode=yes "$1" 'sudo ip -s -s neigh flush all >/dev/null 2>&1' & fi
done
wait
for n in "${NODES[@]}"; do
  set -- $n
  printf "  %-10s %s\n" "$2" "$(ssh -o BatchMode=yes "$2" 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | tr "\n" " "')"
done
