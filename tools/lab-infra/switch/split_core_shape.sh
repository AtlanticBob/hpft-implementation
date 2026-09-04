#!/usr/bin/env bash
# Make the core link (README.md: swp21<->swp25) a bottleneck of ANY rate by
# egress shaping, without breaking the ports out or reloading switchd.
#
#   split_core_shape.sh <Gbps>   shape both directions of the core to <Gbps>
#                                (A->B leaves the switch on swp21, B->A on swp25)
#   split_core_shape.sh off      remove the shaper: the core is the 800G link again
#   split_core_shape.sh status   show what is bound now
#
# Ethernet has no link speed between 200G and 400G; the shaper gives one, and
# it is a real queue, so with an ECN profile bound to the same ports it marks
# like a congested link would. The profile is "core_shape"; port-max-rate is
# in kbps. Every validation run records the bound value in
# results/<tag>/core_shaper.txt.
set -eu
case "${1:?Gbps|off|status}" in
off)
  ssh -o BatchMode=yes sn5600 '
    nv unset interface swp21 qos egress-shaper 2>/dev/null || true
    nv unset interface swp25 qos egress-shaper 2>/dev/null || true
    nv config apply -y >/dev/null
    for p in swp21 swp25; do printf "%s: " $p; nv show interface $p qos egress-shaper 2>/dev/null | grep -iE "profile|port-max" | tr -s " " | tr "\n" " "; echo; done' 2>&1 | grep -v Welcome ;;
status)
  ssh -o BatchMode=yes sn5600 '
    for p in swp21 swp25; do printf "%s: " $p; nv show interface $p qos egress-shaper 2>/dev/null | grep -iE "profile|port-max" | tr -s " " | tr "\n" " "; echo; done' 2>&1 | grep -v Welcome ;;
*)
  g=$1; [ "$g" -gt 0 ] 2>/dev/null || { echo "usage: $0 <Gbps>|off|status"; exit 1; }
  kbps=$((g * 1000000))
  ssh -o BatchMode=yes sn5600 "
    nv set qos egress-shaper core_shape port-max-rate $kbps
    nv set interface swp21,swp25 qos egress-shaper profile core_shape
    nv config apply -y >/dev/null
    for p in swp21 swp25; do printf '%s: ' \$p; nv show interface \$p qos egress-shaper 2>/dev/null | grep -iE 'profile|port-max' | tr -s ' ' | tr '\n' ' '; echo; done" 2>&1 | grep -v Welcome ;;
esac
