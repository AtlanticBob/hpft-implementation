#!/usr/bin/env bash
# The lab's standing QoS state on sn5600: ECN thresholds and the egress queue
# depth that decides when a congested port starts dropping.
#
#   set_qos.sh apply     write the standing state and show it
#   set_qos.sh status    show what is set now
#
# ECN lives in its own profile hpft_ecn, bound to the six ports of the split
# switch (README.md): K_min 800 KB, K_max 3200 KB, P_max 20%. motiv_default_ecn
# is deliberately left untouched - the motivation bundles were measured against
# it and must stay reproducible.
#
# The queue depth is the dynamic threshold alpha_1 instead of the platform's
# alpha_8. alpha_n lets one congested queue take n/(1+n) of the 70.47 MB egress
# user-data pool, so alpha_8 was about 62 MB and alpha_1 is 35.2 MB (23.5 MB
# each when two queues on the port are congested at once).
#
# Everything in this lab rides traffic-class 0. Nothing sets a DSCP, so RDMA and
# TCP land in the same egress queue and share one buffer, which is what makes
# them comparable; the egress queue counters confirm it (traffic-class 1-7 carry
# nothing but the switch's own control frames). Both classes are therefore set,
# but only the traffic-class 0 line has any effect on measurements.
#
# Why the depth matters: a RoCE flow-set holds at most ~512 KB of unacknowledged
# data per QP, so its queue at a bottleneck is capped by QP count, not by offered
# load. Against a 62 MB threshold the V8 core scenario could not reach the drop
# point at any offered load - the port marked ECN forever and never dropped, and
# a fence that reads loss as its evidence got nothing to work with (design v4
# s8.1). V8_core now carries 24 QPs per flow-set so the queue can pass 35.2 MB.
#
# Do not use shared-bytes for the depth: NVUE accepts a static value, never
# programs it, and a shared-alpha set alongside it silently wins.
#
# Buffer settings are switch-wide: this NVUE has one advance-buffer-config
# profile (default-global) and no per-port binding, so host ports change too.
set -eu
PORTS=swp21,swp25,swp37s0,swp37s1,swp3s1,swp4s1

show() {
  ssh -o BatchMode=yes sn5600 '
    echo "== ECN profile bound per port =="
    for p in swp21 swp25 swp37s0 swp37s1 swp3s1 swp4s1; do
      printf "%-9s " $p
      nv show interface $p qos congestion-control 2>/dev/null | grep -i "^ *profile" | tr -s " "
    done
    echo "== hpft_ecn thresholds (traffic-class, ecn, red, min, max, prob) =="
    nv show qos congestion-control hpft_ecn 2>/dev/null | grep -E "^ +[0-9]"
    echo "== egress queue depth as the hardware sees it, swp21 =="
    nv show interface swp21 qos buffer 2>/dev/null \
      | sed -n "/Buffer - Egress Traffic Class/,/Egress Multicast/p" | sed -n "3,7p"
    echo "== egress user-data pool =="
    nv show qos buffer 2>/dev/null | grep -E "^ +13 +0 "
  ' 2>&1 | grep -v Welcome || true
}

case "${1:?apply|status}" in
status) show ;;
apply)
  ssh -o BatchMode=yes sn5600 "
    for tc in 0 3; do
      nv set qos congestion-control hpft_ecn traffic-class \$tc ecn enable
      nv set qos congestion-control hpft_ecn traffic-class \$tc red disable
      nv set qos congestion-control hpft_ecn traffic-class \$tc min-threshold 800000
      nv set qos congestion-control hpft_ecn traffic-class \$tc max-threshold 3200000
      nv set qos congestion-control hpft_ecn traffic-class \$tc probability 20
    done
    nv set qos advance-buffer-config default-global egress-lossy-buffer traffic-class 0 shared-alpha alpha_1
    nv set interface $PORTS qos congestion-control profile hpft_ecn
    nv config apply -y >/dev/null" 2>&1 | grep -v Welcome || true
  show ;;
*) echo "usage: $0 apply|status"; exit 1 ;;
esac
