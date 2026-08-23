#!/usr/bin/env bash
# Emulate a bottleneck HPFT does not own, to check the CC still governs
# there (design.md 1.3: the core is explicitly outside HPFT's guarantee).
#
# A port shaper on the switch egress toward the receiver is narrower than
# the capacity the receiver's allocator believes it has: the DPU port still
# reports 100G, so c_root stays ~97G, the root never reads saturated, and
# the executor therefore sees edge_sat=0 and lifts the floor. The CC term
# is then the only thing standing between the flows and a queue nobody
# divides - which is exactly the condition the low floor exists for.
#
#   on <gbps>   bind swp37s0 to a port shaper at <gbps>
#   off         unbind and delete the profile
#   status      show the binding
# Only swp37s0 is touched; the switch is shared.
set -eu
P=hpft_core_shaper
case "${1:-status}" in
on)
  R=$((${2:-60} * 1000000))    # port-max-rate is in kbps
  ssh -tt sn5600 "nv set qos egress-shaper $P port-max-rate $R
    nv set interface swp37s0 qos egress-shaper profile $P
    nv config apply -y
    nv show interface swp37s0 qos egress-shaper" ;;
off)
  ssh -tt sn5600 "nv unset interface swp37s0 qos egress-shaper
    nv unset qos egress-shaper $P
    nv config apply -y
    nv show interface swp37s0 qos egress-shaper 2>&1 | tail -3" ;;
status)
  ssh -tt sn5600 'nv show interface swp37s0 qos egress-shaper 2>&1 | tail -5' ;;
esac 2>&1 | grep -v Welcome | grep -v "Connection to"
