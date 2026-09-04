#!/usr/bin/env bash
# The resident VxLAN overlay: a static FULL MESH with split horizon.
#
# Generalized overlay (P0 spike, results/p0_overlay_20260729): every VF pair,
# VF IPs untouched, telemetry on p1, tos=inherit (the ECN bit has to survive
# encapsulation or the tenant CC sees a lossless network).
#
# Why a mesh and why it is static (2026-09-04). Until now the overlay was a
# STAR hubbed on the receiver: one tunnel per spoke, every spoke-to-spoke
# frame relayed through the hub's eswitch. A star has one receiver by
# construction; with the switch split in two (tools/lab-infra/switch/), a
# second receiver on the hub's side would have its traffic cross the core
# twice and load the hub's port. A mesh has no hub, but a mesh of tunnels on
# one OVS bridge driven by NORMAL (learn + flood) has no split horizon: a
# broadcast entering on one tunnel is flooded out of the others, comes home by
# a second path, and every remote MAC is learned on the wrong port - measured
# on four nodes (2026-08-23) as instant loss of all VF connectivity. So the
# mesh does not learn or flood across tunnels at all. Forwarding is written
# down from the registry, one rule per VF address (cookie 0x4d45, "ME"):
#
#   200  in_port=<tunnel>, null src or dst MAC        -> drop   (key shaping, below)
#   150  in_port=<tunnel>, arp to a LOCAL VF          -> that VF's representor
#   122/121/120  ip to a LOCAL VF (vf_caps.sh)        -> meter, that VF's representor
#   115  anything else entering on a tunnel           -> drop   (split horizon)
#   110  ip / arp from a local VF to a REMOTE VF      -> the tunnel to its host
#   50/45/40, 0  rx_agent classification, NORMAL      (local traffic only)
#
# Nothing that enters on a tunnel ever leaves on a tunnel, so no loop can
# form; nothing addressed to a remote VF is ever flooded, so no MAC is ever
# learned across tunnels. Local VF to local VF stays NORMAL as before. ARP
# from a local VF is delivered unicast to the one peer that owns the address
# (110), so the hosts keep ordinary dynamic ARP and no static entries are
# needed. Every rule is offloadable (encap, decap, drop, output).
#
# Delivery to a local VF is an explicit output too (vf_caps.sh), because the
# bridge never learns a local VF's MAC once its outbound traffic bypasses
# NORMAL, and NORMAL would then flood every inbound packet to every port in
# a megaflow that hardware refuses (measured 2026-09-04: 0.8 G). Explicit
# output has one side effect: no rule examines the MACs any more, so the
# datapath megaflow key carries an empty eth(), and the receiver agent, which
# reads "which sender VF to which local VF" from exactly the eth(src,dst) of
# that key, sees no flow-set at all. The priority-200 rule exists for that:
# to decide it the classifier must look at both MACs of every tunnel frame,
# and the key keeps them. Same technique as the three per-class meter rules
# in vf_caps.sh, which keep the L4 fields in the key for the same reader.
#
# usage: overlay.sh                       mesh (the resident shape)
#        overlay.sh --rules-only          reinstall the mesh rules, tunnels untouched
#        overlay.sh --star --hub HOST     the old star, kept as the fallback
#        overlay.sh --teardown            direct data path (no overlay)
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
cd "$REPO"
REG=config/lab-registry.json
BR=ovsbr-p1
COOKIE=0x4d45
HUB=""; MODE=mesh
while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB=$2; MODE=star; shift 2 ;;
    --star) MODE=star; shift ;;
    --rules-only) MODE=rules; shift ;;
    --teardown) MODE=teardown; shift ;;
    *) echo "overlay.sh: unknown argument $1"; exit 1 ;;
  esac
done
# how many VFs a host carries is a registry fact (8 since 2026-08-26), not a loop bound
NVF=$(python3 -c "import json;r=json.load(open('$REG'));print(max(len([v for v in r['vnics'] if v['host']==n['host']]) for n in r['nodes']))")
VFIDX=$(seq -s ' ' 0 $((NVF-1)))

mapfile -t NODES < <(python3 -c "
import json
for i, n in enumerate(json.load(open('$REG'))['nodes'], 1):
    print(n['host'], n['dpu'], n['underlay_ip'], n['telemetry_ip'], i)")

# tunnel name on node $1 (idx) towards node $2 (idx). The original pair's
# tunnel keeps its name so anything that greps for vxlan100 (overlay_present,
# lab_env.sh) still recognises the overlay.
tname() { if [ "$1" -le 2 ] && [ "$2" -le 2 ]; then echo vxlan100; else echo "vx$2"; fi; }

if [ "$MODE" = teardown ]; then
  # Tear the overlay down and restore the DIRECT data path: delete ovsbr-p1,
  # move the representors back onto underlay-p1, AND put the p1 uplink back
  # into it. The p1 add-back is essential: building the overlay turns p1 into
  # a standalone L3 netdev, so after teardown the bridge has the representors
  # but NO uplink to the wire -- the "cc all correct, data plane totally
  # broken" symptom is exactly p1 missing from the bridge.
  for n in "${NODES[@]}"; do
    set -- $n
    ssh -o BatchMode=yes "$2" "sudo ovs-vsctl --if-exists del-br $BR
      sudo ip addr flush dev p1 2>/dev/null
      sudo ovs-vsctl --may-exist add-port underlay-p1 p1
      for i in $VFIDX; do sudo ovs-vsctl --may-exist add-port underlay-p1 pf1vf\$i; done
      sudo ip link set p1 up" &
  done
  wait
  echo "overlay: torn down -> direct data path"
  exit 0
fi

# The mesh rules for node $1 (host) $2 (idx), as one ovs-ofctl bundle. All
# carry the cookie, so one del-flows by cookie removes exactly this set.
mesh_rules() {
  python3 - "$1" "$2" "$COOKIE" <<'PY'
import json, sys
host, idx, cookie = sys.argv[1], int(sys.argv[2]), sys.argv[3]
r = json.load(open("config/lab-registry.json"))
nodes = {n["host"]: i for i, n in enumerate(r["nodes"], 1)}
def tname(a, b): return "vxlan100" if a <= 2 and b <= 2 else "vx%d" % b
local = [v for v in r["vnics"] if v["host"] == host]
remote = [v for v in r["vnics"] if v["host"] != host]
tunnels = sorted({tname(idx, nodes[v["host"]]) for v in remote})
out = []
for t in tunnels:
    # A frame entering on a tunnel with a null source or destination MAC is
    # invalid and dropped. The rule's real job is what the classifier has to
    # do to decide it: examine both MACs of every tunnel frame, which keeps
    # eth(src, dst) in the datapath megaflow key (see the header).
    out.append("cookie=%s,priority=200,in_port=%s,dl_src=00:00:00:00:00:00,dl_dst=00:00:00:00:00:00,actions=drop" % (cookie, t))
    for v in local:
        out.append("cookie=%s,priority=150,in_port=%s,arp,arp_tpa=%s,actions=output:%s" % (cookie, t, v["ip"], v["representor"]))
    out.append("cookie=%s,priority=115,in_port=%s,actions=drop" % (cookie, t))
for v in remote:
    t = tname(idx, nodes[v["host"]])
    out.append("cookie=%s,priority=110,ip,nw_dst=%s,actions=output:%s" % (cookie, v["ip"], t))
    out.append("cookie=%s,priority=110,arp,arp_tpa=%s,actions=output:%s" % (cookie, v["ip"], t))
print("\n".join(out))
PY
}

install_rules() { # $1 host $2 dpu $3 idx ; star mode installs none
  local rules=""
  [ "$MODE" = star ] || rules=$(mesh_rules "$1" "$3")
  ssh -o BatchMode=yes "$2" "sudo ovs-ofctl -O OpenFlow13 del-flows $BR cookie=$COOKIE/-1
    if [ -n \"\$(printf '%s' '$rules')\" ]; then printf '%s\n' '$rules' | sudo ovs-ofctl -O OpenFlow13 add-flows $BR -; fi
    sudo ovs-appctl fdb/flush $BR >/dev/null 2>&1
    echo \"  $1 ($2): \$(sudo ovs-ofctl -O OpenFlow13 dump-flows $BR cookie=$COOKIE/-1 | grep -c priority) mesh rules\""
}

if [ "$MODE" = rules ]; then
  for n in "${NODES[@]}"; do set -- $n; install_rules "$1" "$2" "$5" & done; wait
  exit 0
fi

if [ "$MODE" = star ]; then
  HUB=${HUB:-$(python3 -c "import json;print(json.load(open('$REG'))['receiver_host'])")}
  HUB_IDX=$(for n in "${NODES[@]}"; do set -- $n; [ "$1" = "$HUB" ] && echo "$5"; done)
  [ -n "${HUB_IDX:-}" ] || { echo "overlay.sh: $HUB is not a node"; exit 1; }
  echo "== VxLAN overlay: STAR (fallback shape), hub=$HUB =="
else
  echo "== VxLAN overlay: static mesh, split horizon (idempotent) =="
fi

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
      sudo ovs-vsctl --may-exist add-br $BR
      for i in $VFIDX; do
        sudo ovs-vsctl --if-exists del-port underlay-p1 pf1vf\$i 2>/dev/null
        sudo ovs-vsctl --if-exists del-port ovsbr2 pf1vf\$i 2>/dev/null
        sudo ovs-vsctl --may-exist add-port $BR pf1vf\$i
      done
      sudo ip link set $BR up"
  ) &
done
wait

# tunnels: mesh = one to every other node; star = hub to each spoke, spoke to hub
for n in "${NODES[@]}"; do
  set -- $n
  host=$1; dpu=$2; ul=$3; idx=$5
  want=""
  for m in "${NODES[@]}"; do
    set -- $m
    [ "$5" = "$idx" ] && continue
    if [ "$MODE" = star ]; then
      { [ "$idx" = "$HUB_IDX" ] || [ "$5" = "$HUB_IDX" ]; } || continue
    fi
    want="$want $5:$3"
  done
  (
    keep=""
    for w in $want; do
      peer=${w%%:*}; pip=${w#*:}
      name=$(tname "$idx" "$peer")
      keep="$keep $name"
      ssh -o BatchMode=yes "$dpu" "sudo ovs-vsctl --may-exist add-port $BR $name -- set interface $name \
        type=vxlan options:local_ip=$ul options:remote_ip=$pip options:key=100 options:dst_port=4789 options:tos=inherit"
    done
    for pt in $(ssh -o BatchMode=yes "$dpu" "sudo ovs-vsctl list-ports $BR 2>/dev/null | grep -E '^(vx|vxlan)'"); do
      echo " $keep " | grep -q " $pt " || ssh -o BatchMode=yes "$dpu" "sudo ovs-vsctl --if-exists del-port $BR $pt"
    done
    install_rules "$host" "$dpu" "$idx"
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
  printf "  %-10s %s\n" "$2" "$(ssh -o BatchMode=yes "$2" "sudo ovs-vsctl list-ports $BR 2>/dev/null | tr '\n' ' '")"
done
