#!/usr/bin/env bash
# Restore the HPFT lab's RUNTIME state after a DPU reboot or fw reset.
#
# What is volatile and therefore lives here:
#   - VF netdevs (dpu1vf0-3) on every host;
#   - the TCP EDT actuator (root fq + the pinned BPF egress prog), lost when
#     a VF netdev is recreated. Without it TCP is UNPACED, blasts the link,
#     and the resulting congestion crushes RDMA (which obeys ECN) -- this
#     looked like "SR crushes RDMA 0.19" until it was found;
#   - p1's addresses (underlay 172.16.1.x + telemetry 10.1.9.x live DIRECTLY
#     on p1 since 2026-07-29), its 9000 MTU, its LINK SPEED, and the static
#     telemetry ARP. Without the telemetry IP the tx agent gets nothing, the
#     RP fail-opens, and RDMA runs to line;
#   - PFC off on p1.
# The OVS bridge structure (ovsbr-p1 + the tunnels) is NOT volatile and is
# lab_env.sh's; the vport meter, pace shim and qpn resolver are systemd units.
#
# Every node is treated the same and the list comes from the registry, so a
# machine joining the lab is one edit in `nodes`, not a new branch here.
#
# LINK SPEED IS ASSERTED, NOT ASSUMED. Every p1 runs at 200G, and on at least
# one card that is not what it negotiates on its own; the previous version of
# this script pinned the receiver back to 100G on every recovery, which would
# now silently halve the receiver's capacity under a four-node incast while
# the policy plane kept scheduling against 200G.
#
# Run on any host with ssh to the rest, as a sudo-capable user. Idempotent.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
cd "$REPO"
LINE_GBPS=200

mapfile -t NODES < <(python3 -c "
import json
for n in json.load(open('config/lab-registry.json'))['nodes']:
    print(n['host'], n['dpu'], n['underlay_ip'], n['telemetry_ip'])")
[ ${#NODES[@]} -gt 0 ] || { echo "reboot_recover: no nodes in the registry"; exit 1; }

on_host() { # on_host <host> <command>
  if [ "$1" = "$(hostname)" ]; then bash -c "$2"; else ssh -o BatchMode=yes "$1" "$2"; fi
}

echo "== 1/5 VFs on ${#NODES[@]} hosts =="
for n in "${NODES[@]}"; do
  set -- $n
  ( on_host "$1" "bash $REPO/tools/lab-infra/vf_setup.sh >/dev/null 2>&1" \
      || echo "  WARN $1: vf_setup failed" ) &
done
wait
# tenant TCP CC choices used by the CC-matrix experiments must be loadable
for n in "${NODES[@]}"; do set -- $n; on_host "$1" 'sudo modprobe tcp_bbr 2>/dev/null; true' & done
wait

echo "== 2/5 TCP EDT (fq + BPF) on every host =="
for n in "${NODES[@]}"; do
  set -- $n
  ( on_host "$1" "sudo bash $REPO/tools/host/edt_ensure.sh >/dev/null 2>&1 && bash $REPO/tools/host/edt_maps_ensure.sh >/dev/null 2>&1" \
      || echo "  WARN $1: EDT re-apply failed (run tcp-shaper-apply first if the pins are gone)" ) &
done
wait

echo "== 3/5 p1 volatile state: ${LINE_GBPS}G, MTU 9000, underlay + telemetry IPs, PFC off =="
for n in "${NODES[@]}"; do
  set -- $n
  ( ssh -o BatchMode=yes "$2" "
      sudo ip addr add $3/24 dev p1 2>/dev/null
      sudo ip addr add $4/24 dev p1 2>/dev/null
      sudo ip link set p1 up mtu 9000
      cur=\$(cat /sys/class/net/p1/speed 2>/dev/null)
      [ \"\$cur\" = $((LINE_GBPS*1000)) ] || sudo ethtool -s p1 speed $((LINE_GBPS*1000)) duplex full autoneg on 2>/dev/null
      sudo mlnx_qos -i p1 --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1; true" ) &
done
wait
# static telemetry ARP, full mesh: the receiver sends to every sender and any
# sender may become the receiver, so every pair needs the entry.
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

echo "== 4/5 host services + the meter =="
for n in "${NODES[@]}"; do
  set -- $n
  # the shim re-seeds pair_state on startup, so it must follow the EDT re-apply
  ( on_host "$1" 'systemctl is-active hpft-pace-shim >/dev/null 2>&1 && sudo systemctl restart hpft-pace-shim; true'
    # vport_meter holds a DEVX context: a fw reset kills the context but not
    # the process, so is-active lies. Restart unconditionally where enabled.
    ssh -o BatchMode=yes "$2" 'systemctl is-enabled hpft-vport-meter >/dev/null 2>&1 && { sudo systemctl reset-failed hpft-vport-meter 2>/dev/null; sudo systemctl restart hpft-vport-meter; }; true' ) &
done
wait

echo "== 5/5 sanity =="
for n in "${NODES[@]}"; do
  set -- $n
  sp=$(ssh -o BatchMode=yes "$2" 'cat /sys/class/net/p1/speed 2>/dev/null')
  printf "  %-7s %-10s p1=%sG mtu=%s  telemetry reach:" "$1" "$2" "$((${sp:-0}/1000))" \
      "$(ssh -o BatchMode=yes "$2" 'cat /sys/class/net/p1/mtu' 2>/dev/null)"
  for m in "${NODES[@]}"; do
    set -- $m
    [ "$2" = "$(echo $n | awk '{print $2}')" ] && continue
    printf " %s=%s" "$1" "$(ssh -o BatchMode=yes "$(echo $n | awk '{print $2}')" "ping -c1 -W1 $4 >/dev/null 2>&1 && echo ok || echo FAIL" 2>/dev/null)"
  done
  echo
done
echo "== done -- roles.sh assigns the planes; runners restart agents + RP =="
