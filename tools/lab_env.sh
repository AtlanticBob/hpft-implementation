#!/usr/bin/env bash
# lab_env.sh -- one command to switch the lab between its three environments.
# Run on the SENDER host (sgpu01) as a sudo-capable user.
#
#   lab_env.sh status      show which environment is currently active (safe)
#   lab_env.sh hpft        PCC+HPFT production stack: UPCC=1, doca_pcc, rx/tx
#                          agents, pace-shim, DIRECT topology, in-band telemetry
#   lab_env.sh plain       firmware DCQCN baseline: UPCC=0, no HPFT, no Jakiro,
#                          DIRECT topology -- the motivation-experiment baseline
#   lab_env.sh jakiro      firmware DCQCN + VxLAN overlay + Jakiro DHTB at the
#                          receiver decap point (the motiv-1.3 comparison env)
#
# It COMPOSES the authoritative sub-scripts rather than re-implementing them:
#   - cc_mode.sh dcqcn|pcc   the CC / HPFT-stack axis (does the ~5min fw reset
#                            only when UPCC actually has to flip; else skips it)
#   - reboot_recover.sh      direct-topology + telemetry restore (via cc_mode pcc)
# and INLINES the data-plane topology axis (VxLAN build/teardown, VF IP scheme,
# Jakiro start/stop), so this file is the single source of truth for switching.
#
# The three environments differ on FOUR axes, not just CC mode:
#   axis            hpft              plain             jakiro
#   UPCC / CC       1 / PCC device    0 / fw DCQCN      0 / fw DCQCN
#   HPFT agents     running           stopped           stopped
#   data plane      direct(underlay-  direct            VxLAN(ovsbr-p1 +
#                   p1 bridge)                           vxlan100)
#   Jakiro DHTB     no                no                yes (at decap)
#   VF IPs          vf_i=10.1.i.x     same              src 10.1.0.{11..14}
#                                                       -> dst 10.1.0.2
#   p1              100G/underlay     100G              200G/underlay IP on p1
#
# Idempotent: re-running the same target is safe; the fw reset is skipped when
# UPCC is already correct. Getting from jakiro to hpft/plain requires tearing
# the VxLAN down first (a fw reset alone does NOT: OVS config persists across
# resets, so an orphaned ovsbr-p1 keeps the VF representors off underlay-p1 and
# the direct data path stays broken -- teardown_overlay handles this).
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
CC="$REPO/tools/cc_mode.sh"
MST=/dev/mst/mt41692_pciconf0
JAKIRO_DIR=/home/ubuntu/bzx/jakiro_dhtb          # on hpft-dpu2
UL_SENDER=172.16.1.1; UL_RECV=172.16.1.2         # VxLAN underlay on p1

# ---------------------------------------------------------------- status ----
upcc_of() { ssh "$1" "sudo mlxconfig -d $MST q USER_PROGRAMMABLE_CC 2>/dev/null" \
  | awk '/USER_PROGRAMMABLE_CC/{print $NF}' | tr -dc '01'; }

status() {
  local u1 u2 pcc rx tx shim ovs jak
  u1=$(upcc_of hpft-dpu); u2=$(upcc_of hpft-dpu2)
  pcc=$(ssh hpft-dpu 'pgrep -c -x doca_pcc' 2>/dev/null || echo 0)
  rx=$(ssh hpft-dpu2 'systemctl is-active hpft-rxagent-e' 2>/dev/null)
  tx=$(ssh hpft-dpu 'systemctl is-active hpft-txagent-e' 2>/dev/null)
  shim=$(systemctl is-active hpft-pace-shim 2>/dev/null)
  ovs=$(ssh hpft-dpu2 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | tr "\n" "," ')
  jak=$(ssh hpft-dpu2 'pgrep -cf "build/jakiro_dht[b]" || true' 2>/dev/null | head -1)
  jak=${jak:-0}
  echo "UPCC:            dpu=$u1 dpu2=$u2   (1=PCC device, 0=firmware DCQCN)"
  echo "HPFT agents:     rx=$rx tx=$tx shim=$shim  doca_pcc=$pcc"
  echo "ovsbr-p1 ports:  ${ovs:-<none>}"
  echo "jakiro_dhtb:     $([ "$jak" -gt 0 ] && echo running || echo stopped)"
  echo -n "receiver VF IPs: "
  ssh sgpu02 'for i in 0 1 2 3; do printf "vf%s=%s " $i "$(ip -4 addr show dpu1vf$i 2>/dev/null | grep -oE "inet [0-9.]+" | awk "{print \$2}" | head -1)"; done; echo'
  # verdict
  if [ "$u1" = 1 ] && [ "$rx" = active ] && [ "$tx" = active ]; then
    echo "==> environment: HPFT"
  elif ssh hpft-dpu2 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | grep -q vxlan100'; then
    echo "==> environment: JAKIRO (or its VxLAN topology)  jakiro=$([ "$jak" -gt 0 ] && echo up || echo DOWN)"
  else
    echo "==> environment: PLAIN (firmware DCQCN, direct)"
  fi
}

# ------------------------------------------------------------ jakiro ctl ----
jakiro_stop() {
  ssh hpft-dpu2 'pid=$(pgrep -f "build/jakiro_dht[b]" | head -1)
    [ -n "$pid" ] && { sudo kill "$pid" 2>/dev/null; for i in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done; sudo kill -9 "$pid" 2>/dev/null; }
    sudo rm -f '"$JAKIRO_DIR"'/run/jakiro_dhtb.pid; true'
}
jakiro_start() {
  # hardened: kill -> wipe stale DPDK runtime (a fast relaunch over the old
  # instance shared memory fails EAL init "Cannot init memzone") -> apply
  for attempt in 1 2 3; do
    timeout 90 ssh -n hpft-dpu2 'cd '"$JAKIRO_DIR"'
      pid=$(pgrep -f "build/jakiro_dht[b]" | head -1)
      [ -n "$pid" ] && { sudo kill -9 "$pid" 2>/dev/null; sleep 1; }
      sudo rm -f run/jakiro_dhtb.pid; sudo rm -rf /var/run/dpdk/rte; sudo rm -f /dev/hugepages/rtemap_*
      set -a; . ./jakiro_dhtb.conf; set +a
      sudo -E bash apply_jakiro_dhtb.sh >/dev/null 2>&1 || true
      sleep 3
      pgrep -f "build/jakiro_dht[b]" >/dev/null && grep -aq "DHTB_CFG" run/jakiro_dhtb.log' \
      && { echo "  jakiro up (attempt $attempt)"; return 0; }
    echo "  jakiro start attempt $attempt failed, retrying"; sleep 3
  done
  echo "  jakiro start FAILED" >&2; return 1
}

# --------------------------------------------------- overlay build/teardown -
# Build the VxLAN overlay + move VF representors onto ovsbr-p1. Mirrors
# 1-3_20260716/setup_topology.sh; single overlay IP (Jakiro variant).
build_overlay() {
  echo "== build VxLAN overlay (ovsbr-p1 + vxlan100) =="
  _setup_dpu hpft-dpu  "$UL_SENDER" "$UL_RECV"
  _setup_dpu hpft-dpu2 "$UL_RECV"   "$UL_SENDER"
  # receiver vf0 is the single overlay IP; vf1-3 idle
  ssh sgpu02 'for i in 1 2 3; do sudo ip addr flush dev dpu1vf$i 2>/dev/null; done
    sudo ip addr flush dev dpu1vf0 2>/dev/null; sudo ip addr add 10.1.0.2/24 dev dpu1vf0; sudo ip link set dpu1vf0 up'
  # sender VFs all on the overlay subnet 10.1.0.{11..14}
  for i in 0 1 2 3; do
    sudo ip addr flush dev dpu1vf$i 2>/dev/null
    sudo ip addr add 10.1.0.$((11+i))/24 dev dpu1vf$i; sudo ip link set dpu1vf$i up
  done
  ssh hpft-dpu2 'sudo ethtool -s p1 speed 200000 duplex full 2>/dev/null'
  sudo ip -s -s neigh flush all >/dev/null 2>&1; ssh sgpu02 'sudo ip -s -s neigh flush all >/dev/null 2>&1'
}
_setup_dpu() { # host local_ip remote_ip
  ssh "$1" "sudo ip addr flush dev underlay-p1 2>/dev/null; sudo ip addr flush dev p1 2>/dev/null
    sudo ip addr add $2/24 dev p1; sudo ip link set p1 up mtu 9000
    sudo ovs-vsctl --if-exists del-br ovsbr-p1; sudo ovs-vsctl add-br ovsbr-p1
    for i in 0 1 2 3; do sudo ovs-vsctl --if-exists del-port underlay-p1 pf1vf\$i; sudo ovs-vsctl --may-exist add-port ovsbr-p1 pf1vf\$i; done
    sudo ovs-vsctl add-port ovsbr-p1 vxlan100 -- set interface vxlan100 type=vxlan options:local_ip=$2 options:remote_ip=$3 options:key=100 options:dst_port=4789
    sudo ip link set ovsbr-p1 up"
}
# Tear the overlay down and restore the DIRECT data path: delete ovsbr-p1,
# move the VF representors back onto underlay-p1, drop the underlay IP from p1.
# (reboot_recover.sh restores the telemetry IP + VFs but NOT representor
# placement, so this inverse of build_overlay is mandatory before hpft/plain.)
teardown_overlay() {
  echo "== teardown VxLAN overlay -> direct data path =="
  for h in hpft-dpu hpft-dpu2; do
    ssh "$h" 'sudo ovs-vsctl --if-exists del-br ovsbr-p1
      for i in 0 1 2 3; do sudo ovs-vsctl --may-exist add-port underlay-p1 pf1vf$i; done
      sudo ip addr flush dev p1 2>/dev/null; sudo ip link set p1 up'
  done
  # restore direct VF IPs on both hosts (sender vf_i=10.1.i.1, recv vf_i=10.1.i.2)
  for i in 0 1 2 3; do sudo ip addr flush dev dpu1vf$i 2>/dev/null; sudo ip addr add 10.1.$i.1/24 dev dpu1vf$i; sudo ip link set dpu1vf$i up; done
  ssh sgpu02 'for i in 0 1 2 3; do sudo ip addr flush dev dpu1vf$i 2>/dev/null; sudo ip addr add 10.1.$i.2/24 dev dpu1vf$i; sudo ip link set dpu1vf$i up; done'
}

overlay_present() { ssh hpft-dpu2 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | grep -q vxlan100'; }

# ------------------------------------------------------------- targets ------
case "${1:-}" in
status) status ;;

hpft)
  echo "== -> HPFT =="
  jakiro_stop
  if overlay_present; then teardown_overlay; fi
  bash "$CC" pcc                       # UPCC=1 (+fw reset if needed) + reboot_recover + RP + agents + shim
  echo "== HPFT ready =="; status ;;

plain)
  echo "== -> PLAIN (firmware DCQCN baseline, direct) =="
  jakiro_stop
  bash "$CC" dcqcn                     # stop HPFT, UPCC=0 (+fw reset if needed) + post_recover (direct VFs)
  if overlay_present; then teardown_overlay; fi   # after dcqcn: fw reset does NOT wipe OVS
  echo "== PLAIN ready =="; status ;;

jakiro)
  echo "== -> JAKIRO (firmware DCQCN + VxLAN + DHTB) =="
  bash "$CC" dcqcn                     # stop HPFT, UPCC=0
  build_overlay
  jakiro_start
  echo "== JAKIRO ready =="; status ;;

*) grep '^#' "$0" | sed -n '2,14p' ;;
esac
