#!/usr/bin/env bash
# Stage 3 of splitting sn5600 in two (README.md): run AFTER the loopback cable
# is plugged and split_verify.sh switch shows swp21/swp25 up and forwarding.
# Moves the four host ports out of VLAN 100 into the two sides. One apply;
# traffic between side A and side B breaks for a few seconds.
#   side A (VLAN 101): swp37s1 = sgpu01, swp3s1 = sgpu03
#   side B (VLAN 102): swp37s0 = sgpu02 (receiver), swp4s1 = sgpu04
set -eu
ssh -o BatchMode=yes sn5600 '
  for p in swp21 swp25; do
    st=$(nv show interface --view=brief 2>/dev/null | awk -v p=$p "\$1==p{print \$3}")
    [ "$st" = up ] || { echo "ABORT: $p oper state is [$st] - plug the cable and run split_verify.sh switch first"; exit 1; }
  done
  nv set interface swp37s1,swp3s1 bridge domain br_default access 101
  nv set interface swp37s0,swp4s1 bridge domain br_default access 102
  nv config apply -y
  echo "== host ports"
  for p in swp37s1 swp3s1 swp37s0 swp4s1; do printf "%s access vlan: " $p; nv show interface $p bridge domain br_default access 2>/dev/null | tail -1; done' 2>&1 | grep -v Welcome
