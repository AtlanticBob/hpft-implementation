#!/usr/bin/env bash
# cc_mode.sh -- switch the lab between CC modes / retransmission modes.
# Run on sgpu01. See docs/cc_mode_switching.md for background.
#
#   cc_mode.sh status            show firmware flags, services, link state
#   cc_mode.sh gbn | sr          INSTANT retrans switch via ROCE_ACCL
#                                (selective_repeat_forced_en, both hosts;
#                                no fw reset; DCQCN-compatible; volatile --
#                                re-apply after any fw reset)
#   cc_mode.sh dcqcn [gbn|sr]    non-PCC firmware DCQCN baseline:
#                                stops HPFT/PCC, UPCC=0 (+optional retrans)
#   cc_mode.sh pcc               PCC+HPFT stack: UPCC=1 + fw reset, then
#                                reboot_recover + RP + agents + pace-shim
#                                (soak cron is NOT touched either way)
#
# GBN/SR (2026-07-15 final): forced SR via the ROCE_ACCL access register is
# THE method -- one mlxreg command per host, applies to all new QPs, keeps
# firmware DCQCN untouched. The RDMA_SELECTIVE_REPEAT_EN mlxconfig flag is
# unrelated to it and stays 0. See docs/cc_mode_switching.md.
#
# Facts this script encodes (probed 2026-07-14, see docs):
#  - USER_PROGRAMMABLE_CC=1 with no doca_pcc app != DCQCN (internal DPA algo);
#  - mlxconfig changes need mlxfwreset --sync 1 from the HOST (Arm reboot
#    does NOT activate them); the Arm reboots as a side effect (~4 min);
#  - after any fw reset the VFs, MTU, 100G bottleneck, pause/PFC must be
#    re-applied -- this script does all of that.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2
MST=/dev/mst/mt41692_pciconf0
VF_MTU=${VF_MTU:-1500}

q() { # q <host> -> "UPCC_cur SR_cur UPCC_next SR_next"
  ssh "$1" "sudo mlxconfig -e -d $MST q USER_PROGRAMMABLE_CC RDMA_SELECTIVE_REPEAT_EN 2>/dev/null" \
   | awk '/USER_PROGRAMMABLE_CC/{uc=$(NF-1);un=$NF} /RDMA_SELECTIVE_REPEAT_EN/{sc=$(NF-1);sn=$NF}
          END{gsub(/[^01()]/,"",uc);gsub(/[^01()]/,"",un);gsub(/[^01()]/,"",sc);gsub(/[^01()]/,"",sn);
              print uc,sc,un,sn}' | tr -d '()'
}

status() {
  for h in hpft-dpu hpft-dpu2; do
    read -r uc sc un sn <<<"$(q $h)"
    echo "$h: UPCC current=$uc next=$un | SR current=$sc next=$sn"
  done
  echo "doca_pcc on dpu1: $(ssh hpft-dpu 'pgrep -c -x doca_pcc' 2>/dev/null || echo 0)"
  echo "txagent=$(ssh hpft-dpu 'systemctl is-active hpft-txagent-e' 2>/dev/null) rxagent=$(ssh hpft-dpu2 'systemctl is-active hpft-rxagent-e' 2>/dev/null) shim=$(systemctl is-active hpft-pace-shim 2>/dev/null)"
  echo "dpu2 p1: $(ssh hpft-dpu2 'ethtool p1 2>/dev/null | grep Speed')"
  echo "swp37s0 ECN profile: $(ssh sn5600 'nv show interface swp37s0 qos congestion-control 2>/dev/null' 2>/dev/null | grep -v Welcome | awk '/profile/{print $3}' | head -1)"
  ibdev2netdev 2>/dev/null | grep -c dpu1vf | xargs echo "VFs on sgpu01:"
}

fw_reset_both() {
  echo "== fw reset both DPUs (parallel, ~4-5 min; Arms reboot) =="
  sudo bash -c 'echo 0 > /sys/bus/pci/devices/0000:38:00.1/sriov_numvfs; sleep 2; mlxfwreset -d 38:00.0 --yes --sync 1 reset' &
  P1=$!
  ssh sgpu02 "sudo bash -c 'echo 0 > /sys/bus/pci/devices/0000:38:00.1/sriov_numvfs; sleep 2; mlxfwreset -d 38:00.0 --yes --sync 1 reset'" &
  P2=$!
  wait $P1 $P2
  echo "== waiting for both Arms =="
  until ssh -o ConnectTimeout=5 -o BatchMode=yes hpft-dpu 'echo x' >/dev/null 2>&1 \
     && ssh -o ConnectTimeout=5 -o BatchMode=yes hpft-dpu2 'echo x' >/dev/null 2>&1; do sleep 15; done
}

post_recover() {
  echo "== VFs + MTU $VF_MTU + 100G bottleneck + lossy =="
  bash "$REPO/tools/lab-infra/vf_setup.sh" >/dev/null 2>&1 || true
  # sgpu02 keeps its own independent copy at this path (separate host,
  # outside this repo's checkout) -- not touched by the local repo-relative
  # path above.
  ssh sgpu02 'bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1'
  sleep 4
  for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu "$VF_MTU" 2>/dev/null; done
  ssh sgpu02 "for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set \$d mtu $VF_MTU 2>/dev/null; done"
  ssh hpft-dpu2 'sudo ethtool -s p1 speed 100000 duplex full 2>/dev/null'
  for h in hpft-dpu hpft-dpu2; do
    ssh $h 'for p in p0 p1; do sudo ethtool -A $p rx off tx off 2>/dev/null; sudo mlnx_qos -i $p --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1; done'
  done
}

stop_hpft() {
  echo "== stopping HPFT/PCC (pace-shim, agents, RP) =="
  sudo systemctl stop hpft-pace-shim 2>/dev/null
  ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo pkill -9 -x doca_pcc; true'
  ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
}

set_flags() { # set_flags <UPCC or -> <SR or ->  ; returns 0 if a reset is needed
  local upcc=$1 sr=$2 need=0
  for h in hpft-dpu hpft-dpu2; do
    read -r uc sc un sn <<<"$(q $h)"
    local args=""
    [ "$upcc" != "-" ] && [ "$uc" != "$upcc" ] && args="$args USER_PROGRAMMABLE_CC=$upcc"
    [ "$sr"   != "-" ] && [ "$sc" != "$sr"   ] && args="$args RDMA_SELECTIVE_REPEAT_EN=$sr"
    # also fix stale next-boot values even when current is right
    [ "$upcc" != "-" ] && [ "$un" != "$upcc" ] && args="$args USER_PROGRAMMABLE_CC=$upcc"
    [ "$sr"   != "-" ] && [ "$sn" != "$sr"   ] && args="$args RDMA_SELECTIVE_REPEAT_EN=$sr"
    args=$(echo "$args" | tr ' ' '\n' | sort -u | tr '\n' ' ')
    if [ -n "${args// }" ]; then
      ssh $h "sudo mlxconfig -y -d $MST set $args >/dev/null 2>&1"
    fi
    [ "$upcc" != "-" ] && [ "$uc" != "$upcc" ] && need=1
    [ "$sr"   != "-" ] && [ "$sc" != "$sr"   ] && need=1
  done
  return $((1-need))
}

case "${1:-}" in
status) status ;;
gbn|sr)
  # instant per-port switch via ROCE_ACCL.selective_repeat_forced_en
  # (no fw reset; volatile -- re-apply after any fw reset/reboot).
  # applies to ALL QPs incl. plain (non-CM) ones; DCQCN unaffected.
  want=0; [ "$1" = sr ] && want=1
  sudo mlxreg -y -d 38:00.1 --reg_name ROCE_ACCL --set "selective_repeat_forced_en=$want" >/dev/null 2>&1
  ssh sgpu02 "sudo mlxreg -y -d 38:00.1 --reg_name ROCE_ACCL --set 'selective_repeat_forced_en=$want' >/dev/null 2>&1"
  echo -n "sgpu01: "; sudo mlxreg -d 38:00.1 --reg_name ROCE_ACCL --get 2>/dev/null | grep 'selective_repeat_forced_en '
  echo -n "sgpu02: "; ssh sgpu02 'sudo mlxreg -d 38:00.1 --reg_name ROCE_ACCL --get 2>/dev/null | grep "selective_repeat_forced_en "'
  echo "retrans mode: $1 (takes effect for newly created QPs; existing QPs unaffected)" ;;
dcqcn)
  stop_hpft
  if set_flags 0 -; then fw_reset_both; post_recover; else echo "flags already current; skipped fw reset"; fi
  if [ -n "${2:-}" ]; then bash "$0" "$2"; fi
  status
  echo "NOTE: soak/watchdog cron NOT touched -- comment them out for controlled runs." ;;
pcc)
  if set_flags 1 -; then fw_reset_both; fi
  bash $REPO/tools/reboot_recover.sh
  ssh hpft-dpu 'bash /tmp/rp_service.sh start' | tail -1
  ssh hpft-dpu2 'sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py'
  sleep 3
  ssh hpft-dpu 'sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py'
  sudo systemctl reset-failed hpft-pace-shim 2>/dev/null
  systemctl is-active hpft-pace-shim >/dev/null 2>&1 || sudo systemd-run --unit hpft-pace-shim --property=Restart=always /usr/bin/python3 $REPO/tools/host/hpft_pace_shim.py
  status
  echo "NOTE: soak/watchdog cron NOT touched -- re-enable manually if wanted." ;;
*) grep '^#' "$0" | sed -n '2,12p' ;;
esac
