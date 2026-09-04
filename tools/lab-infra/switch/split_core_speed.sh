#!/usr/bin/env bash
# The core link (README.md: swp21<->swp25) runs at exactly one of two speeds.
#
#   split_core_speed.sh 200      core = 200G (the standing setting)
#   split_core_speed.sh 400      core = 400G
#   split_core_speed.sh status   show what is set
#
# Both are done by egress shaping on the two loop ports (profile core_speed,
# port-max-rate in kbps), not by breaking the ports out: shaping takes effect
# in one apply with no switchd reload and no host-port flap, and the shaper
# was calibrated with RDMA (30G -> 29.6 G wire, 150G -> 148 G wire). No other
# value is accepted - the lab's two settings are 200 and 400. Every validation
# run records the setting in results/<tag>/core_speed.txt.
set -eu
case "${1:?200|400|status}" in
status)
  ssh -o BatchMode=yes sn5600 '
    for p in swp21 swp25; do printf "%s: " $p; nv show interface $p qos egress-shaper 2>/dev/null | grep -iE "port-max-rate" | tr -s " " | tr "\n" " "; echo; done' 2>&1 | grep -v Welcome ;;
200|400)
  kbps=$(( $1 * 1000000 ))
  ssh -o BatchMode=yes sn5600 "
    nv set qos egress-shaper core_speed port-max-rate $kbps
    nv set interface swp21,swp25 qos egress-shaper profile core_speed
    nv config apply -y >/dev/null
    for p in swp21 swp25; do printf '%s: ' \$p; nv show interface \$p qos egress-shaper 2>/dev/null | grep -iE 'port-max-rate' | tr -s ' ' | tr '\n' ' '; echo; done" 2>&1 | grep -v Welcome ;;
*) echo "usage: $0 200|400|status (no other core speed exists in this lab)"; exit 1 ;;
esac
