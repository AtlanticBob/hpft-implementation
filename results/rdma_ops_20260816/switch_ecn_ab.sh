#!/usr/bin/env bash
# ECN threshold A/B for the RDMA execution-plane limit cycle.
#   define   recreate profile ecn_incast_bzx (TC0+TC3, Kmin 100000 / Kmax
#            400000 / 20%, red off) exactly as it existed at switch rev 373;
#            definition only, no binding changes
#   shallow  bind swp37s0 -> ecn_incast_bzx      (07-28 regression conditions)
#   deep     bind swp37s0 -> motiv_default_ecn   (eval plan v3 §1.3 default)
#   status   show binding + both definitions
# Only swp37s0 is touched (our port; the switch is shared, 28 ports in use).
set -eu
case "${1:-status}" in
define)
  ssh sn5600 'for tc in 0 3; do
    nv set qos congestion-control ecn_incast_bzx traffic-class $tc ecn enable
    nv set qos congestion-control ecn_incast_bzx traffic-class $tc red disable
    nv set qos congestion-control ecn_incast_bzx traffic-class $tc min-threshold 100000
    nv set qos congestion-control ecn_incast_bzx traffic-class $tc max-threshold 400000
    nv set qos congestion-control ecn_incast_bzx traffic-class $tc probability 20
  done; nv config apply -y >/dev/null 2>&1; nv show qos congestion-control ecn_incast_bzx' ;;
shallow)
  ssh sn5600 'nv set interface swp37s0 qos congestion-control profile ecn_incast_bzx; nv config apply -y >/dev/null 2>&1; nv show interface swp37s0 qos congestion-control | grep profile' ;;
deep)
  ssh sn5600 'nv set interface swp37s0 qos congestion-control profile motiv_default_ecn; nv config apply -y >/dev/null 2>&1; nv show interface swp37s0 qos congestion-control | grep profile' ;;
status)
  ssh sn5600 'nv show interface swp37s0 qos congestion-control | grep profile; nv show qos congestion-control ecn_incast_bzx 2>/dev/null; nv show qos congestion-control motiv_default_ecn' ;;
esac 2>&1 | grep -v Welcome
