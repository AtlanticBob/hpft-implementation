#!/usr/bin/env bash
# Assign the HPFT planes to nodes.
#
# Every DPU carries both planes (deploy_check.sh keeps the payload uniform),
# so "which node is the receiver" is a per-experiment decision, not a
# property of the lab. This is where that decision is made and made
# visible; before it existed the answer was buried in cc_mode.sh's
# hardcoded hpft-dpu / hpft-dpu2, which is why the lab could only ever be
# 1 sender -> 1 receiver.
#
# The units stay TRANSIENT (systemd-run), as they have always been: a
# transient unit is generated from one command line every time, so there is
# no unit file on any node to drift from what this script says. The role a
# node holds is readable at any time from `roles.sh status`.
#
#   roles.sh status
#   roles.sh set --receiver sgpu02 --senders sgpu01,sgpu03,sgpu04
#   roles.sh stop
#
# The receiver additionally runs the vport meter (its class-split counter
# source); senders additionally run the host-side pace shim for the TCP
# executor. Nothing here touches CC mode - that is still cc_mode.sh.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
REG=config/lab-registry.json
PY="python3 -c"

dpu_of() { $PY "
import json;print({n['host']:n['dpu'] for n in json.load(open('$REG'))['nodes']}.get('$1',''))"; }
all_hosts() { $PY "
import json;print(' '.join(n['host'] for n in json.load(open('$REG'))['nodes']))"; }

stop_all() {
  for h in $(all_hosts); do
    d=$(dpu_of "$h")
    ( ssh -o BatchMode=yes "$d" 'sudo systemctl stop hpft-rxagent-e hpft-txagent-e 2>/dev/null
        sudo systemctl reset-failed hpft-rxagent-e hpft-txagent-e 2>/dev/null; true' >/dev/null 2>&1
      if [ "$h" = "$(hostname)" ]; then
        sudo systemctl stop hpft-pace-shim hpft-qpn-resolver 2>/dev/null; sudo systemctl reset-failed hpft-pace-shim hpft-qpn-resolver 2>/dev/null
      else
        ssh -o BatchMode=yes "$h" 'sudo systemctl stop hpft-pace-shim hpft-qpn-resolver 2>/dev/null; sudo systemctl reset-failed hpft-pace-shim hpft-qpn-resolver 2>/dev/null; true' >/dev/null 2>&1
      fi ) &
  done
  wait
}

start_receiver() { # $1 = host
  local h=$1 d; d=$(dpu_of "$h")
  [ -n "$d" ] || { echo "roles: $h is not in $REG nodes"; exit 1; }
  # meter before agent: the agent prefers the meter's class split and falls
  # back on staleness, so starting it second means the first ticks silently
  # run on the fallback chain.
  ssh -o BatchMode=yes "$d" "sudo install -m644 /opt/hpft/hpft-vport-meter.service /etc/systemd/system/ && sudo systemctl daemon-reload
    sudo systemctl enable --now hpft-vport-meter 2>/dev/null
    sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl
    sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py --local-host $h" >/dev/null 2>&1 \
    && echo "  receiver  $h ($d): rxagent + vport-meter" || echo "  FAILED receiver $h ($d)"
}

start_sender() { # $1 = host
  local h=$1 d; d=$(dpu_of "$h")
  [ -n "$d" ] || { echo "roles: $h is not in $REG nodes"; exit 1; }
  # RP FIRST, and restarted even if it is already up. The executor does not
  # survive its sender agent being replaced: after a tx agent restart the RP
  # keeps taking budgets and answering (rc=0, the level reads back correct)
  # but stops applying the rate to the wire - measured 52.9 G against a 20 G
  # grant, and 18.5 G for the same grant the moment the RP was restarted
  # (2026-08-23). cc_mode.sh always did this implicitly by calling
  # rp_service.sh on its way in, which is why the pair was never seen to
  # fail; a role switch that restarts agents alone reproduces it every time.
  ssh -o BatchMode=yes "$d" "bash /opt/hpft/rp_service.sh start" >/dev/null 2>&1 \
    || echo "  WARN $h ($d): RP restart failed - the executor will not pace"
  ssh -o BatchMode=yes "$d" "sudo systemctl reset-failed hpft-txagent-e 2>/dev/null
    sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py --local-host $h" >/dev/null 2>&1 \
    && echo "  sender    $h ($d): RP + txagent --local-host $h" || echo "  FAILED sender $h ($d)"
  # Two host-side services, both required for a sender to actually be one:
  #   pace-shim     the TCP executor's map writer
  #   qpn-resolver  {qpn -> pair} for the RDMA executor. Without it the RP
  #                 cannot tell which QPs belong to the pair, qp_count stays
  #                 1, and every QP of an n-QP flow is handed the pair's
  #                 WHOLE budget - n times the granted share on the wire,
  #                 with the ledger, the law and the mailbox all reading
  #                 correct. Starting it is not optional for a sender.
  local shim='sudo systemctl reset-failed hpft-pace-shim 2>/dev/null; systemctl is-active hpft-pace-shim >/dev/null 2>&1 || sudo systemd-run --unit hpft-pace-shim --property=Restart=always /usr/bin/python3 REPOPATH/tools/host/hpft_pace_shim.py'
  local qpn='sudo systemctl reset-failed hpft-qpn-resolver 2>/dev/null; systemctl is-active hpft-qpn-resolver >/dev/null 2>&1 || sudo systemd-run --unit hpft-qpn-resolver --property=Restart=always /usr/bin/python3 REPOPATH/tools/host/qpn_resolver.py --local-host HOSTNAME --peer PEERHOST'
  qpn=${qpn/HOSTNAME/$h}; qpn=${qpn/PEERHOST/$RECEIVER}
  if [ "$h" = "$(hostname)" ]; then
    eval "${shim/REPOPATH/$REPO}" >/dev/null 2>&1
    eval "${qpn/REPOPATH/$REPO}" >/dev/null 2>&1
  else
    ssh -o BatchMode=yes "$h" "${shim/REPOPATH/$REPO}" >/dev/null 2>&1
    ssh -o BatchMode=yes "$h" "${qpn/REPOPATH/$REPO}" >/dev/null 2>&1
  fi
}

status() {
  printf "%-8s %-11s %-9s %-9s %-11s %-9s %s\n" HOST DPU RXAGENT TXAGENT VPORTMETER SHIM QPNRES
  for h in $(all_hosts); do
    d=$(dpu_of "$h")
    read -r rx tx vm <<<"$(ssh -o BatchMode=yes "$d" 'printf "%s %s %s" "$(systemctl is-active hpft-rxagent-e 2>/dev/null)" "$(systemctl is-active hpft-txagent-e 2>/dev/null)" "$(systemctl is-active hpft-vport-meter 2>/dev/null)"' 2>/dev/null)"
    if [ "$h" = "$(hostname)" ]; then
      sh=$(systemctl is-active hpft-pace-shim 2>/dev/null); qr=$(systemctl is-active hpft-qpn-resolver 2>/dev/null)
    else
      read -r sh qr <<<"$(ssh -o BatchMode=yes "$h" 'printf "%s %s" "$(systemctl is-active hpft-pace-shim 2>/dev/null)" "$(systemctl is-active hpft-qpn-resolver 2>/dev/null)"' 2>/dev/null)"
    fi
    printf "%-8s %-11s %-9s %-9s %-11s %-9s %s\n" "$h" "$d" "${rx:-?}" "${tx:-?}" "${vm:-?}" "${sh:-?}" "${qr:-?}"
  done
}

case "${1:-status}" in
  status) status ;;
  stop)   stop_all; echo "roles: all planes stopped"; status ;;
  set)
    shift; RECV=""; SENDERS=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --receiver) RECV=$2; shift 2 ;;
        --senders)  SENDERS=$(echo "$2" | tr ',' ' '); shift 2 ;;
        *) echo "roles: unknown argument $1"; exit 1 ;;
      esac
    done
    [ -n "$RECV" ] || { echo "roles: --receiver is required"; exit 1; }
    # senders default to every node that is not the receiver: with a
    # symmetric payload that is the only default that does not quietly
    # shrink the lab back to the pair it used to be.
    [ -n "$SENDERS" ] || SENDERS=$(for h in $(all_hosts); do [ "$h" = "$RECV" ] || echo -n "$h "; done)
    echo "== roles: receiver=$RECV senders=$SENDERS =="
    stop_all
    RECEIVER=$RECV
    start_receiver "$RECV"
    for s in $SENDERS; do start_sender "$s"; done
    sleep 2; status ;;
  *) echo "usage: roles.sh status | stop | set --receiver <host> [--senders h1,h2]"; exit 1 ;;
esac
