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
# Who is who comes from the registry: the lab is four machines and any of
# them can be the receiver, so a name written here would be a second opinion.
SENDER=${HPFT_SENDER:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['sender_host'])")}
RECEIVER=${HPFT_RECEIVER:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['receiver_host'])")}
all_hosts() { python3 -c "
import json;print(' '.join(n['host'] for n in json.load(open('$REPO/config/lab-registry.json'))['nodes']))"; }
all_dpus()  { python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('$REPO/config/lab-registry.json'))['nodes']))"; }
dpu_of()    { python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('$REPO/config/lab-registry.json'))['nodes']}['$1'])"; }

upcc_of() { ssh "$1" "sudo mlxconfig -d $MST q USER_PROGRAMMABLE_CC 2>/dev/null" \
  | awk '/USER_PROGRAMMABLE_CC/{print $NF}' | tr -dc '01'; }

status() {
  local u1 pcc rx tx jak
  local SDPU RDPU
  SDPU=$(dpu_of "$SENDER"); RDPU=$(dpu_of "$RECEIVER")
  u1=$(upcc_of "$SDPU")
  pcc=$(ssh "$SDPU" 'pgrep -c -x doca_pcc 2>/dev/null; true' 2>/dev/null | head -1)
  rx=$(ssh "$RDPU" 'systemctl is-active hpft-rxagent-e' 2>/dev/null)
  tx=$(ssh "$SDPU" 'systemctl is-active hpft-txagent-e' 2>/dev/null)
  jak=$(ssh "$RDPU" 'pgrep -cf "build/jakiro_dht[b]" || true' 2>/dev/null | head -1)
  jak=${jak:-0}
  # UPCC, doca_pcc and the planes, per node -- with four machines a single
  # dpu/dpu2 line cannot say which of them is misconfigured.
  bash "$REPO/tools/cc_mode.sh" status
  echo -n "overlay ports:   "; for n in $(all_dpus); do printf "%s[%s] " "$n" "$(ssh "$n" 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | tr "\n" "," ' 2>/dev/null)"; done; echo
  echo "jakiro_dhtb:     $([ "$jak" -gt 0 ] && echo running || echo stopped)"
  echo "doca_pcc bin:    $(ssh "$SDPU" 'pgrep -a -x doca_pcc | head -1 | sed "s/^[0-9]* //"' 2>/dev/null || echo none)"
  echo "rx meters:       $(ssh "$RDPU" 'sudo ovs-ofctl -O OpenFlow13 dump-meters ovsbr-p1 2>/dev/null | grep -c "meter=1[1-4]" || true' 2>/dev/null)"
  echo -n "receiver VF IPs: "
  ssh "$RECEIVER" 'for i in 0 1 2 3; do printf "vf%s=%s " $i "$(ip -4 addr show dpu1vf$i 2>/dev/null | grep -oE "inet [0-9.]+" | awk "{print \$2}" | head -1)"; done; echo'
  # verdict
  if [ "$u1" = 1 ] && [ "$rx" = active ] && [ "$tx" = active ]; then
    echo "==> environment: HPFT"
  elif [ "$u1" = 1 ] && ssh "$SDPU" 'pgrep -a -x doca_pcc | grep -q pcc_ztr_stock' 2>/dev/null; then
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
ensure_overlay() { bash "$REPO/tools/lab-infra/overlay.sh" --hub "$RECEIVER"; }
teardown_overlay() {
  bash "$REPO/tools/lab-infra/overlay.sh" --teardown
  # restore direct VF IPs on every host
  for h in $(all_hosts); do
    if [ "$h" = "$(hostname)" ]; then
      for i in 0 1 2 3; do sudo ip addr flush dev dpu1vf$i 2>/dev/null; done
      bash "$REPO/tools/lab-infra/vf_setup.sh" >/dev/null 2>&1
    else
      ssh "$h" "bash $REPO/tools/lab-infra/vf_setup.sh >/dev/null 2>&1"
    fi
  done
}

overlay_present() { ssh "$(dpu_of "$RECEIVER")" 'sudo ovs-vsctl list-ports ovsbr-p1 2>/dev/null | grep -qE "^(vx|vxlan)"'; }

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
