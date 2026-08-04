#!/usr/bin/env bash
# lab_env.sh -- one command to switch the lab between its environments.
# Run on the SENDER host (sgpu01) as a sudo-capable user.
#
# OVERLAY-RESIDENT (2026-07-29, P0 spike + user decision): the generalized
# VxLAN overlay is the STANDING data plane for ALL environments -- both DPUs
# keep ovsbr-p1 + vxlan100 (tos=inherit) with the VF representors on it, p1
# carries the underlay IP (172.16.1.x) plus the telemetry IP (10.1.9.x)
# directly, VF IPs stay on the direct scheme (10.1.i.1 <-> 10.1.i.2), p1
# stays at 100G. Environments now differ only on the CC/agents/DHTB axes:
#
#   lab_env.sh status      show which environment is currently active (safe)
#   lab_env.sh hpft        PCC+HPFT production stack: UPCC=1, doca_pcc, rx/tx
#                          agents, pace-shim, in-band telemetry
#   lab_env.sh plain       firmware DCQCN baseline: UPCC=0, no HPFT, no Jakiro
#   lab_env.sh jakiro      firmware DCQCN + Jakiro DHTB at the receiver decap
#                          point (multi-sender flows need cross_pair_net.sh)
#   lab_env.sh ztr         UPCC=1 + STOCK DOCA PCC RTT template (ZTR-RTTCC),
#                          no HPFT agents -- the native-ZTR arm of the eval
#                          CC matrix (binary: ~/bzx/pcc_ztr_stock on the DPU,
#                          built from the pristine SDK application source)
#   lab_env.sh direct      LEGACY/debug: tear the overlay down to the old
#                          direct topology (p1 + reps back on underlay-p1)
#   lab_env.sh meter on [gbps] | off | status
#                          per-dst-VF OVS meters on the receiver vswitch
#                          (drop band, default 30G) -- the naive-policer
#                          downlink cap of the native/with-TC arms
#
#   axis            hpft              plain             jakiro
#   UPCC / CC       1 / PCC device    0 / fw DCQCN      0 / fw DCQCN
#   HPFT agents     running           stopped           stopped
#   Jakiro DHTB     no                no                yes (at decap)
#   data plane      -------- shared VxLAN overlay (ensure_overlay) --------
#
# It COMPOSES the authoritative sub-scripts rather than re-implementing them:
#   - cc_mode.sh dcqcn|pcc   the CC / HPFT-stack axis (does the ~5min fw reset
#                            only when UPCC actually has to flip; else skips it)
#   - reboot_recover.sh      volatile overlay state (p1 IPs/MTU/ARP) + VFs +
#                            EDT + telemetry (via cc_mode pcc)
#
# Idempotent: re-running the same target is safe. OVS bridge structure
# PERSISTS across fw resets / Arm reboots; only p1 addresses, MTU and static
# ARP are volatile (reboot_recover / ensure_overlay re-assert them). The
# rx agent's default --bridge is ovsbr-p1 since the overlay became resident.
# tos=inherit on vxlan100 is MANDATORY: without it the inner DSCP is not
# copied to the outer header, tclass-steered RDMA lands in switch TC0 and
# the with-TC arm is impossible (P0 gate 5); ECN fold-back (RFC 6040) was
# verified intact both with and without it (P0 gate 4).
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
CC="$REPO/tools/cc_mode.sh"
MST=/dev/mst/mt41692_pciconf0
JAKIRO_DIR=/home/ubuntu/bzx/jakiro_dhtb          # on hpft-dpu2
ZTR_BIN=/home/ubuntu/bzx/pcc_ztr_stock/build/pcc/doca_pcc   # on hpft-dpu
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
  echo "doca_pcc bin:    $(ssh hpft-dpu 'pgrep -a -x doca_pcc | head -1 | sed "s/^[0-9]* //"' 2>/dev/null || echo none)"
  echo "rx meters:       $(ssh hpft-dpu2 'sudo ovs-ofctl -O OpenFlow13 dump-meters ovsbr-p1 2>/dev/null | grep -c "meter=1[1-4]" || true' 2>/dev/null)"
  echo -n "receiver VF IPs: "
  ssh sgpu02 'for i in 0 1 2 3; do printf "vf%s=%s " $i "$(ip -4 addr show dpu1vf$i 2>/dev/null | grep -oE "inet [0-9.]+" | awk "{print \$2}" | head -1)"; done; echo'
  # verdict
  if [ "$u1" = 1 ] && [ "$rx" = active ] && [ "$tx" = active ]; then
    echo "==> environment: HPFT"
  elif [ "$u1" = 1 ] && ssh hpft-dpu 'pgrep -a -x doca_pcc | grep -q pcc_ztr_stock' 2>/dev/null; then
    echo "==> environment: ZTR (stock RTT template, no HPFT)"
  elif [ "$jak" -gt 0 ]; then
    echo "==> environment: JAKIRO (DHTB up)"
  else
    # The overlay is RESIDENT since 2026-07-29, so vxlan100's presence no
    # longer identifies the environment (it used to mean "jakiro"); the
    # remaining axes are UPCC / agents / DHTB.
    echo "==> environment: PLAIN (firmware DCQCN)"
  fi
  ssh hpft-dpu2 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | grep -q vxlan100' \
    && echo "    data plane:  resident VxLAN overlay" \
    || echo "    data plane:  DIRECT (legacy; rx agent defaults to ovsbr-p1)"
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

# --------------------------------------------------- overlay ensure/teardown
# Generalized overlay (P0 spike, results/p0_overlay_20260729): all 4 VF pairs,
# VF IPs untouched, telemetry on p1, tos=inherit, p1 stays 100G. Idempotent:
# full build only when vxlan100 is missing; the volatile parts (p1 IPs, MTU,
# ARP, representor membership, tos option) are re-asserted on every call.
ensure_overlay() {
  echo "== ensure VxLAN overlay (ovsbr-p1 + vxlan100, tos=inherit) =="
  if ! overlay_present; then
    _setup_dpu hpft-dpu  "$UL_SENDER" "$UL_RECV" 10.1.9.1
    _setup_dpu hpft-dpu2 "$UL_RECV"   "$UL_SENDER" 10.1.9.2
  else
    _assert_dpu hpft-dpu  "$UL_SENDER" 10.1.9.1
    _assert_dpu hpft-dpu2 "$UL_RECV"   10.1.9.2
  fi
  M1=$(ssh hpft-dpu  'cat /sys/class/net/p1/address'); M2=$(ssh hpft-dpu2 'cat /sys/class/net/p1/address')
  ssh hpft-dpu  "sudo arp -s 10.1.9.2 $M2 2>/dev/null; true"
  ssh hpft-dpu2 "sudo arp -s 10.1.9.1 $M1 2>/dev/null; true"
  sudo ip -s -s neigh flush all >/dev/null 2>&1; ssh sgpu02 'sudo ip -s -s neigh flush all >/dev/null 2>&1'
}
_setup_dpu() { # host underlay_local underlay_remote telemetry_ip
  # p1 must leave underlay-p1 and become a standalone L3 netdev: the VxLAN
  # underlay IP sits straight on p1 and the kernel routes encap traffic over
  # it, which an OVS bridge PORT (pure L2) cannot do (also the documented
  # precondition for VxLAN hw offload). The telemetry IP rides p1 as a
  # second address (its old home, the underlay-p1 bridge port, has no uplink
  # in overlay mode). teardown_overlay is the exact inverse.
  ssh "$1" "sudo ovs-vsctl --if-exists del-port underlay-p1 p1
    sudo ip addr flush dev underlay-p1 2>/dev/null; sudo ip addr flush dev p1 2>/dev/null
    sudo ip addr add $2/24 dev p1; sudo ip addr add $4/24 dev p1; sudo ip link set p1 up mtu 9000
    sudo ovs-vsctl --if-exists del-br ovsbr-p1; sudo ovs-vsctl add-br ovsbr-p1
    for i in 0 1 2 3; do sudo ovs-vsctl --if-exists del-port underlay-p1 pf1vf\$i; sudo ovs-vsctl --may-exist add-port ovsbr-p1 pf1vf\$i; done
    sudo ovs-vsctl add-port ovsbr-p1 vxlan100 -- set interface vxlan100 type=vxlan options:local_ip=$2 options:remote_ip=$3 options:key=100 options:dst_port=4789 options:tos=inherit
    sudo ip link set ovsbr-p1 up"
}
_assert_dpu() { # host underlay_local telemetry_ip -- volatile state only
  ssh "$1" "sudo ip addr add $2/24 dev p1 2>/dev/null
    sudo ip addr add $3/24 dev p1 2>/dev/null
    sudo ip link set p1 up mtu 9000
    for i in 0 1 2 3; do sudo ovs-vsctl --may-exist add-port ovsbr-p1 pf1vf\$i 2>/dev/null; done
    sudo ovs-vsctl set interface vxlan100 options:tos=inherit
    sudo ip link set ovsbr-p1 up; true"
}
# Tear the overlay down and restore the DIRECT data path: delete ovsbr-p1,
# move the VF representors back onto underlay-p1, AND put the p1 uplink back
# into underlay-p1, dropping the underlay L3 IP from p1.
# (reboot_recover.sh restores the telemetry IP + VFs but NOT representor NOR
# uplink placement, so this inverse of build_overlay is mandatory before
# hpft/plain.) The p1 add-back is essential: build_overlay turns p1 into a
# standalone L3 netdev (underlay IP straight on p1), so after teardown the
# underlay-p1 bridge has the representors but NO uplink to the wire -- the
# "cc all correct, data plane totally broken" symptom is exactly p1 missing
# from the bridge (2026-07-24, found by the impl agent; teardown alone did
# not fix a live instance of this state until p1 was re-added).
teardown_overlay() {
  echo "== teardown VxLAN overlay -> direct data path =="
  for h in hpft-dpu hpft-dpu2; do
    ssh "$h" 'sudo ovs-vsctl --if-exists del-br ovsbr-p1
      sudo ip addr flush dev p1 2>/dev/null
      sudo ovs-vsctl --may-exist add-port underlay-p1 p1
      for i in 0 1 2 3; do sudo ovs-vsctl --may-exist add-port underlay-p1 pf1vf$i; done
      sudo ip link set p1 up'
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
  ensure_overlay
  bash "$CC" pcc                       # UPCC=1 (+fw reset if needed) + reboot_recover + RP + agents + shim
  ensure_overlay                       # fw reset reboots the Arms: re-assert volatile p1 state
  echo "== HPFT ready =="; status ;;

plain)
  echo "== -> PLAIN (firmware DCQCN baseline) =="
  jakiro_stop
  bash "$CC" dcqcn                     # stop HPFT, UPCC=0 (+fw reset if needed) + post_recover
  ensure_overlay                       # after any fw reset / vf_setup: re-assert overlay volatile state
  echo "== PLAIN ready =="; status ;;

jakiro)
  echo "== -> JAKIRO (firmware DCQCN + DHTB at decap) =="
  bash "$CC" dcqcn                     # stop HPFT, UPCC=0
  ensure_overlay
  jakiro_start
  echo "== JAKIRO ready ==  (multi-sender -> 10.1.0.2 flows need cross_pair_net.sh apply)"; status ;;

direct)
  echo "== -> DIRECT (legacy topology, debug only) =="
  jakiro_stop
  if overlay_present; then teardown_overlay; fi
  echo "== DIRECT ready (rx agent default bridge is ovsbr-p1: pass --bridge underlay-p1 by hand) ==" ;;

ztr)
  echo "== -> ZTR (stock RTT template, UPCC=1, no HPFT) =="
  jakiro_stop
  ensure_overlay
  if [ "$(upcc_of hpft-dpu)" != 1 ]; then
    bash "$CC" pcc            # brings UPCC=1 (fw reset path); converted below
    ensure_overlay
  fi
  ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
  ssh hpft-dpu  'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
  sudo systemctl stop hpft-pace-shim 2>/dev/null || true
  ssh -n hpft-dpu "sudo pkill -x doca_pcc 2>/dev/null; sleep 1
    sudo setsid nohup $ZTR_BIN -d mlx5_0 -w -1 -l 40 >/tmp/ztr_pcc.log 2>&1 < /dev/null &
    sleep 3; pgrep -ax doca_pcc | head -1" </dev/null
  echo "== ZTR ready (back to hpft: lab_env.sh hpft restarts the HPFT RP+agents+shim) =="
  status ;;

meter)
  RATE_KBPS=$(( ${3:-30} * 1000000 ))
  case "${2:-status}" in
    on)
      ssh hpft-dpu2 "for i in 0 1 2 3; do
        sudo ovs-ofctl -O OpenFlow13 add-meter ovsbr-p1 \"meter=\$((11+i)),kbps,band=type=drop,rate=$RATE_KBPS\" 2>/dev/null
        sudo ovs-ofctl -O OpenFlow13 add-flow ovsbr-p1 \"priority=120,ip,in_port=vxlan100,nw_dst=10.1.\$i.2,actions=meter:\$((11+i)),NORMAL\"
      done
      echo -n 'meters installed: '; sudo ovs-ofctl -O OpenFlow13 dump-meters ovsbr-p1 | grep -c 'meter=1[1-4]'" ;;
    off)
      ssh hpft-dpu2 'for i in 0 1 2 3; do
        sudo ovs-ofctl -O OpenFlow13 del-flows ovsbr-p1 "ip,in_port=vxlan100,nw_dst=10.1.$i.2" 2>/dev/null
        sudo ovs-ofctl -O OpenFlow13 del-meter ovsbr-p1 "meter=$((11+i))" 2>/dev/null
      done; echo "meters removed"' ;;
    status)
      ssh hpft-dpu2 'sudo ovs-ofctl -O OpenFlow13 dump-meters ovsbr-p1 2>/dev/null | grep "meter=1[1-4]" || echo "no meters"
        sudo ovs-ofctl -O OpenFlow13 dump-flows ovsbr-p1 2>/dev/null | grep -o "nw_dst=10.1.[0-3].2 actions=meter:[0-9]*" || true' ;;
    *) echo "usage: lab_env.sh meter on [gbps] | off | status"; exit 2 ;;
  esac ;;

*) grep '^#' "$0" | sed -n '2,26p' ;;
esac
