#!/usr/bin/env bash
# cc_mode.sh -- switch the lab between CC modes / retransmission modes.
# Run on any host with ssh to the rest. See docs/cc_mode_switching.md.
#
#   cc_mode.sh status            show firmware flags, services, link state
#   cc_mode.sh gbn | sr          INSTANT retrans switch via ROCE_ACCL
#                                (selective_repeat_forced_en, every host;
#                                no fw reset; DCQCN-compatible; volatile --
#                                re-apply after any fw reset)
#   cc_mode.sh dcqcn [gbn|sr]    non-PCC firmware DCQCN baseline:
#                                stops HPFT/PCC, UPCC=0 (+optional retrans)
#   cc_mode.sh pcc [receiver]    PCC+HPFT stack: UPCC=1 + fw reset, then
#                                reboot_recover + roles.sh (soak cron is NOT
#                                touched either way)
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
#  - after any fw reset the VFs, MTU, link speed and pause/PFC must be
#    re-applied. That is reboot_recover.sh's job and this script calls it
#    rather than keeping a second copy of it.
#
# Every node comes from the registry's `nodes`, and each host's PF address
# from lab-tcp-registry's pf_bdf: the two card slots in this lab do not share
# a PCI address, and a hardcoded 38:00.1 silently addressed the wrong card on
# half the machines.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
cd "$REPO"
MST=/dev/mst/mt41692_pciconf0
VF_MTU=${VF_MTU:-1500}

mapfile -t NODES < <(python3 -c "
import json
reg=json.load(open('config/lab-registry.json'))
tcp=json.load(open('config/lab-tcp-registry.json'))
pf={v['host']: v['pf_bdf'] for v in tcp['vnics']}
for n in reg['nodes']:
    print(n['host'], n['dpu'], pf.get(n['host'], '0000:38:00.1'))")
DEFAULT_RECEIVER=$(python3 -c "
import json;print(json.load(open('config/lab-registry.json'))['receiver_host'])")

on_host() { if [ "$1" = "$(hostname)" ]; then bash -c "$2"; else ssh -o BatchMode=yes "$1" "$2"; fi; }
pf_short() { echo "${1#0000:}"; }          # 0000:38:00.1 -> 38:00.1
pf_fn0()   { echo "${1#0000:}"; }          # caller replaces the .1 when needed

q() { # q <dpu> -> "UPCC_cur SR_cur UPCC_next SR_next"
  ssh -o BatchMode=yes "$1" "sudo mlxconfig -e -d $MST q USER_PROGRAMMABLE_CC RDMA_SELECTIVE_REPEAT_EN 2>/dev/null" \
   | awk '/USER_PROGRAMMABLE_CC/{uc=$(NF-1);un=$NF} /RDMA_SELECTIVE_REPEAT_EN/{sc=$(NF-1);sn=$NF}
          END{gsub(/[^01()]/,"",uc);gsub(/[^01()]/,"",un);gsub(/[^01()]/,"",sc);gsub(/[^01()]/,"",sn);
              print uc,sc,un,sn}' | tr -d '()'
}

status() {
  for n in "${NODES[@]}"; do
    set -- $n
    read -r uc sc un sn <<<"$(q $2)"
    printf "%-7s %-10s UPCC current=%s next=%s | SR current=%s next=%s | doca_pcc=%s | p1=%sG\n" \
      "$1" "$2" "$uc" "$un" "$sc" "$sn" \
      "$(ssh -o BatchMode=yes $2 'pgrep -c -x doca_pcc 2>/dev/null; true' 2>/dev/null | head -1)" \
      "$(( $(ssh -o BatchMode=yes $2 'cat /sys/class/net/p1/speed' 2>/dev/null || echo 0) / 1000 ))"
  done
  bash "$REPO/tools/lab-infra/roles.sh" status
  echo "swp37s0 ECN profile: $(ssh sn5600 'nv show interface swp37s0 qos congestion-control 2>/dev/null' 2>/dev/null | grep -v Welcome | awk '/profile/{print $3}' | head -1)"
}

fw_reset_all() {
  echo "== fw reset ${#NODES[@]} DPUs (parallel, ~4-5 min; Arms reboot) =="
  # ROCE_ACCL is volatile across fw reset: capture the SR state now and
  # re-apply it after the Arms return (P12, 2026-07-29 -- eval default is
  # SR=1 and a silent fall-back to GBN corrupted an experiment arm once).
  set -- ${NODES[0]}
  SR_PRE=$(on_host "$1" "sudo mlxreg -y -d $(pf_short $3) --reg_name ROCE_ACCL --get 2>/dev/null" \
           | grep selective_repeat_forced_en | grep -q '0x00000001' && echo 1 || echo 0)
  for n in "${NODES[@]}"; do
    set -- $n
    on_host "$1" "sudo bash -c 'echo 0 > /sys/bus/pci/devices/$3/sriov_numvfs; sleep 2; mlxfwreset -d ${3%.*}.0 --yes --sync 1 reset'" &
  done
  wait
  echo "== waiting for all Arms =="
  for n in "${NODES[@]}"; do
    set -- $n
    until ssh -o ConnectTimeout=5 -o BatchMode=yes "$2" 'echo x' >/dev/null 2>&1; do sleep 15; done
  done
  if [ "${SR_PRE:-}" = "1" ]; then
    echo "== re-applying SR (ROCE_ACCL was 1 before the reset) =="
    bash "$0" sr
  fi
}

post_recover() {
  echo "== VFs, MTU $VF_MTU, link speed, pause/PFC (reboot_recover) =="
  bash "$REPO/tools/reboot_recover.sh" | sed 's/^/  /'
  for n in "${NODES[@]}"; do
    set -- $n
    on_host "$1" "for d in /sys/class/net/dpu1vf*; do sudo ip link set \$(basename \$d) mtu $VF_MTU 2>/dev/null; done" &
    ssh -o BatchMode=yes "$2" 'for p in p0 p1; do sudo ethtool -A $p rx off tx off 2>/dev/null; sudo mlnx_qos -i $p --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1; done' &
  done
  wait
}

stop_hpft() {
  echo "== stopping HPFT/PCC (agents, shims, resolvers, RP) =="
  bash "$REPO/tools/lab-infra/roles.sh" stop >/dev/null 2>&1
  for n in "${NODES[@]}"; do set -- $n; ssh -o BatchMode=yes "$2" 'sudo pkill -9 -x doca_pcc; true' & done
  wait
}

set_flags() { # set_flags <UPCC or -> <SR or ->  ; returns 0 if a reset is needed
  local upcc=$1 sr=$2 need=0
  for n in "${NODES[@]}"; do
    set -- $n
    read -r uc sc un sn <<<"$(q $2)"
    local args=""
    [ "$upcc" != "-" ] && [ "$uc" != "$upcc" ] && args="$args USER_PROGRAMMABLE_CC=$upcc"
    [ "$sr"   != "-" ] && [ "$sc" != "$sr"   ] && args="$args RDMA_SELECTIVE_REPEAT_EN=$sr"
    # also fix stale next-boot values even when current is right
    [ "$upcc" != "-" ] && [ "$un" != "$upcc" ] && args="$args USER_PROGRAMMABLE_CC=$upcc"
    [ "$sr"   != "-" ] && [ "$sn" != "$sr"   ] && args="$args RDMA_SELECTIVE_REPEAT_EN=$sr"
    args=$(echo "$args" | tr ' ' '\n' | sort -u | tr '\n' ' ')
    if [ -n "${args// }" ]; then
      ssh -o BatchMode=yes "$2" "sudo mlxconfig -y -d $MST set $args >/dev/null 2>&1"
    fi
    [ "$upcc" != "-" ] && [ "$uc" != "$upcc" ] && need=1
    [ "$sr"   != "-" ] && [ "$sc" != "$sr"   ] && need=1
  done
  return $((1-need))
}

restore_sr() {   # $1 = 0|1 ; re-assert the volatile ROCE_ACCL bit on every host
  local want=${1:-0}
  [ "$want" = 1 ] || return 0
  for n in "${NODES[@]}"; do
    set -- $n
    on_host "$1" "sudo mlxreg -y -d $(pf_short $3) --reg_name ROCE_ACCL --set 'selective_repeat_forced_en=1' >/dev/null 2>&1"
  done
  echo "  SR re-applied after fw reset (register is volatile)"
}

case "${1:-}" in
status) status ;;
gbn|sr)
  # instant per-port switch via ROCE_ACCL.selective_repeat_forced_en
  # (no fw reset; volatile -- re-apply after any fw reset/reboot).
  # applies to ALL QPs incl. plain (non-CM) ones; DCQCN unaffected.
  want=0; [ "$1" = sr ] && want=1
  for n in "${NODES[@]}"; do
    set -- $n
    on_host "$1" "sudo mlxreg -y -d $(pf_short $3) --reg_name ROCE_ACCL --set 'selective_repeat_forced_en=$want' >/dev/null 2>&1"
    printf "%-7s " "$1"
    on_host "$1" "sudo mlxreg -d $(pf_short $3) --reg_name ROCE_ACCL --get 2>/dev/null | grep 'selective_repeat_forced_en '"
  done
  echo "retrans mode: $1 (takes effect for newly created QPs; existing QPs unaffected)" ;;
dcqcn)
  stop_hpft
  # ROCE_ACCL is volatile: a fw reset zeroes selective_repeat_forced_en, so
  # a lab standing on SR silently comes back as GBN. Remember and re-apply
  # (an explicit gbn|sr argument still wins, applied just below).
  set -- ${NODES[0]}
  WANT_SR=$(on_host "$1" "sudo mlxreg -y -d $(pf_short $3) --reg_name ROCE_ACCL --get 2>/dev/null" \
            | awk '/^selective_repeat_forced_en  /{print $NF+0}')
  if set_flags 0 -; then fw_reset_all; post_recover; restore_sr "${WANT_SR:-0}"; else echo "flags already current; skipped fw reset"; fi
  if [ -n "${2:-}" ]; then bash "$0" "$2"; fi
  status
  echo "NOTE: soak/watchdog cron NOT touched -- comment them out for controlled runs." ;;
pcc)
  RECV=${2:-$DEFAULT_RECEIVER}
  set -- ${NODES[0]}
  WANT_SR=$(on_host "$1" "sudo mlxreg -y -d $(pf_short $3) --reg_name ROCE_ACCL --get 2>/dev/null" \
            | awk '/^selective_repeat_forced_en  /{print $NF+0}')
  if set_flags 1 -; then fw_reset_all; restore_sr "${WANT_SR:-0}"; fi
  # post_recover, not bare reboot_recover: vf_setup leaves the VFs at MTU
  # 8192 while the DPU representors/bridge are 1500. Any TCP connection that
  # idles past the OVS flow max-idle (10 s) then loses every full-size
  # segment - the first packet after aging must take the slow path through
  # a 1500-byte representor and is dropped as oversized, so the HW flow is
  # never reinstalled (2026-08-26, cost a whole 1-2 ZTR row).
  post_recover
  # roles.sh owns which node runs which plane, and starts the RP before the
  # sender agent -- an executor left over from the previous role assignment
  # takes budgets and stops pacing.
  bash "$REPO/tools/lab-infra/roles.sh" set --receiver "$RECV"
  echo "NOTE: soak/watchdog cron NOT touched -- re-enable manually if wanted." ;;
*) grep '^#' "$0" | sed -n '2,12p' ;;
esac
