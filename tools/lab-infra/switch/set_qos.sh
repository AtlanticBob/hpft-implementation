#!/usr/bin/env bash
# The lab's standing QoS state on sn5600: ECN thresholds and the egress queue
# depth that decides when a congested port starts dropping.
#
#   set_qos.sh apply [notc|tc]   write the standing state and show it
#   set_qos.sh status            show what is set now
#
# ECN lives in its own profile hpft_ecn, bound to the six ports of the split
# switch (README.md): K_min 800 KB, K_max 3200 KB, P_max 20%. motiv_default_ecn
# is deliberately left untouched - the motivation bundles were measured against
# it and must stay reproducible.
#
# THE QUEUE DEPTH IS A FIXED NUMBER OF BYTES, NOT A DYNAMIC THRESHOLD. A queue
# that grows with whatever else happens to be congested is not an experimental
# constant: the same port would give one class 35 MB when it alone is busy and
# 23 MB when its sibling is busy too, so an arm with one queue and an arm with
# two queues could never be compared. The egress user-data service pool is
# therefore in STATIC mode and every traffic class gets an explicit byte quota:
#
#   notc  all traffic in TC0            TC0 = 32 MB
#   tc    RDMA in TC3, TCP in TC0       TC0 = 16 MB, TC3 = 16 MB
#
# Both modes give a congested port the same 32 MB in total, which is what makes
# the with-TC arm comparable to the others.
#
# A static per-class quota only reaches the hardware when the POOL itself is in
# static mode: with the pool dynamic, NVUE accepts shared-bytes, reports it as
# applied, and the hardware keeps showing DYNAMIC with the alpha. Set
# egress-service-pool 0 mode static (memory-percent must be restated in the same
# transaction) and the quota lands - `nv show interface <p> qos buffer` then
# reads Mode STATIC with the byte figure. Always confirm there, never in the
# config readback.
#
# TC3 is a LOSSLESS class on this switch (qos roce mode lossless, PFC priority
# 3), so its quota does not come from egress-lossy-buffer at all - that branch
# is silently ignored for it and the class stays in the infinite lossless pool
# 14. It is placed by egress-lossless-buffer instead, pointed at service-pool 0
# with its own shared-bytes. Switching the whole switch to `qos roce mode lossy`
# would do the same thing but is refused while the default-global PFC profile
# exists (it is still bound to every port outside this experiment).
#
# PFC is off on all six of our ports (motiv-nopfc), so "lossless" here means
# only which pool the class is accounted in; the class drops when its quota is
# full like any other.
#
# Everything in this lab rides traffic-class 0 unless an experiment explicitly
# steers RDMA to TC3 (--tclass=106 -> DSCP 26 -> switch-priority 3). Nothing
# else sets a DSCP, so RDMA and TCP normally land in the same egress queue and
# share one buffer, which is what makes them comparable; the egress queue
# counters confirm it (traffic-class 1-7 carry nothing but control frames).
#
# Why the depth matters: a RoCE flow-set holds at most ~512 KB of unacknowledged
# data per QP, so its queue at a bottleneck is capped by QP count, not by offered
# load. Against a 62 MB threshold the V8 core scenario could not reach the drop
# point at any offered load - the port marked ECN forever and never dropped, and
# a fence that reads loss as its evidence got nothing to work with (design v4
# s8.1).
#
# Buffer settings are switch-wide: this NVUE has one advance-buffer-config
# profile (default-global) and no per-port binding, so host ports change too.
set -eu
PORTS=swp21,swp25,swp37s0,swp37s1,swp3s1,swp4s1
TC0_NOTC=$((32*1024*1024))
TC0_TC=$((16*1024*1024))
TC3_BYTES=$((16*1024*1024))

show() {
  ssh -o BatchMode=yes sn5600 '
    echo "== ECN profile bound per port =="
    for p in swp21 swp25 swp37s0 swp37s1 swp3s1 swp4s1; do
      printf "%-9s " $p
      nv show interface $p qos congestion-control 2>/dev/null | grep -i "^ *profile" | tr -s " "
    done
    echo "== hpft_ecn thresholds (traffic-class, ecn, red, min, max, prob) =="
    nv show qos congestion-control hpft_ecn 2>/dev/null | grep -E "^ +[0-9]"
    echo "== egress queue quota AS THE HARDWARE SEES IT, swp37s0 =="
    nv show interface swp37s0 qos buffer 2>/dev/null \
      | sed -n "/Buffer - Egress Traffic Class/,/Buffer - Egress Multicast/p" | sed -n "3,7p"
    echo "== egress user-data pool =="
    nv show qos buffer 2>/dev/null | grep -E "^ +13 +0 "
  ' 2>&1 | grep -v Welcome || true
}

case "${1:?apply|status}" in
status) show ;;
apply)
  MODE=${2:-notc}
  case "$MODE" in
    notc) TC0=$TC0_NOTC ;;
    tc)   TC0=$TC0_TC ;;
    *) echo "usage: $0 apply [notc|tc]"; exit 1 ;;
  esac
  echo "buffer mode: $MODE  (TC0 = $((TC0/1024/1024)) MB, TC3 = $((TC3_BYTES/1024/1024)) MB)"
  ssh -o BatchMode=yes sn5600 "
    for tc in 0 3; do
      nv set qos congestion-control hpft_ecn traffic-class \$tc ecn enable
      nv set qos congestion-control hpft_ecn traffic-class \$tc red disable
      nv set qos congestion-control hpft_ecn traffic-class \$tc min-threshold 800000
      nv set qos congestion-control hpft_ecn traffic-class \$tc max-threshold 3200000
      nv set qos congestion-control hpft_ecn traffic-class \$tc probability 20
    done
    nv set qos advance-buffer-config default-global egress-service-pool 0 memory-percent 50
    nv set qos advance-buffer-config default-global egress-service-pool 0 mode static
    nv set qos advance-buffer-config default-global egress-lossy-buffer traffic-class 0 shared-bytes $TC0
    nv set qos advance-buffer-config default-global egress-lossless-buffer service-pool 0
    nv set qos advance-buffer-config default-global egress-lossless-buffer shared-bytes $TC3_BYTES
    nv set interface $PORTS qos congestion-control profile hpft_ecn
    nv config apply -y >/dev/null" 2>&1 | grep -v Welcome || true
  show ;;
*) echo "usage: $0 apply [notc|tc] | status"; exit 1 ;;
esac
