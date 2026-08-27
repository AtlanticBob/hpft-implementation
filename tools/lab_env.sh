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
#   lab_env.sh swift       like ztr, but the Swift port of the stock template
#                          (~/bzx/pcc_swift_stock, results/swift_20260827)
#   lab_env.sh ztr         UPCC=1 + STOCK DOCA PCC RTT template (ZTR-RTTCC)
#                          on every sender DPU, no HPFT agents -- the
#                          native-ZTR arm of the CC matrix (binary:
#                          ~/bzx/pcc_ztr_stock on each DPU, built from the
#                          pristine SDK application source)
#   lab_env.sh direct      LEGACY/debug: tear the overlay down to the old
#                          direct topology (p1 + reps back on underlay-p1)
#   lab_env.sh meter on [gbps] | off | status
#                          per-VF OVS drop-band meters on EVERY DPU (the
#                          naive-policer downlink cap; rate = policy MaxRate,
#                          50G since 2026-08-26; see lab-infra/vf_caps.sh)
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
ZTR_BIN=/home/ubuntu/bzx/pcc_ztr_stock/build/pcc/doca_pcc     # stock DOCA RTT template (ZTR-RTTCC), every sender DPU
SWIFT_BIN=/home/ubuntu/bzx/pcc_swift_stock/build/pcc/doca_pcc # same tree with Swift in algorithm_core (results/swift_20260827), every sender DPU
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
  ssh "$RECEIVER" 'for i in $(seq 0 7); do printf "vf%s=%s " $i "$(ip -4 addr show dpu1vf$i 2>/dev/null | grep -oE "inet [0-9.]+" | awk "{print \$2}" | head -1)"; done; echo'
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
      for d in /sys/class/net/dpu1vf*; do sudo ip addr flush dev $(basename $d) 2>/dev/null; done
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

ztr|swift)
  STOCK_BIN=$ZTR_BIN; [ "$1" = swift ] && STOCK_BIN=$SWIFT_BIN
  echo "== -> ${1^^} (stock RTT-template binary $STOCK_BIN, UPCC=1, no HPFT) on every SENDER DPU =="
  jakiro_stop
  ensure_overlay
  # UPCC=1 on all nodes (cc_mode pcc does the fw reset on every registry
  # node); the HPFT stack it starts is torn down right after.
  if [ "$(upcc_of "$(dpu_of "$SENDER")")" != 1 ]; then
    bash "$CC" pcc
    ensure_overlay
  fi
  bash "$REPO/tools/lab-infra/roles.sh" stop >/dev/null 2>&1
  # ZTR runs on the sender DPUs. The RECEIVER needs a PCC application too:
  # with UPCC=1 and no app the NIC answers nothing and every RDMA flow into
  # it dies with "retry counter exceeded" (probed 2026-08-26). The stock
  # RTT-template binary as the responder ALSO kills the flows (0 iterations);
  # the HPFT executor (rp_service.sh, same PCC framework) as the responder
  # works, and with no sender agent it shapes nothing on the receive side.
  ssh -n "$(dpu_of "$RECEIVER")" 'bash /opt/hpft/rp_service.sh start >/dev/null 2>&1; echo -n "  receiver responder: "; pgrep -ax doca_pcc | head -1' </dev/null
  for d in $(all_dpus); do
    [ "$d" = "$(dpu_of "$RECEIVER")" ] && continue
    ssh -n "$d" "sudo pkill -x doca_pcc 2>/dev/null; sleep 1
      sudo setsid nohup $STOCK_BIN -d mlx5_0 -w -1 -l 40 >/tmp/ztr_pcc.log 2>&1 < /dev/null &
      sleep 3; echo -n '  $d: '; pgrep -ax doca_pcc | head -1" </dev/null
  done
  echo "== ${1^^} ready (back to plain/hpft: lab_env.sh plain|hpft; re-run vf_caps.sh sync + cc_mode.sh gbn|sr + cross_pair_net after the fw reset) =="
  status ;;

meter)
  # Per-VF downlink policer on EVERY DPU is standing lab shape since
  # 2026-08-26 (50G per VF, tools/lab-infra/vf_caps.sh). This target is kept
  # as a thin alias so older runners keep working; "on [gbps]" re-installs
  # the meters at the policy rate (the gbps argument is ignored with a note).
  case "${2:-status}" in
    on)  [ -n "${3:-}" ] && echo "  note: meter rate comes from the registry policy (max_rate_bps), argument $3 ignored"
         bash "$REPO/tools/lab-infra/vf_caps.sh" meter-on ;;
    off) bash "$REPO/tools/lab-infra/vf_caps.sh" meter-off ;;
    status) bash "$REPO/tools/lab-infra/vf_caps.sh" status ;;
    *) echo "usage: lab_env.sh meter on | off | status"; exit 2 ;;
  esac ;;

*) grep '^#' "$0" | sed -n '2,26p' ;;
esac
