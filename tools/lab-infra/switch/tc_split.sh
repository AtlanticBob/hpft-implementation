#!/usr/bin/env bash
# The with-TC arm's class weights: how the switch divides a congested port
# between RDMA (traffic-class 3) and TCP (traffic-class 0).
#
#   tc_split.sh status
#   tc_split.sh set <rdma-percent>     e.g. 50 for 1:1, 60 for 3 RDMA tenants
#                                      against 2 TCP tenants
#
# The weights live in the ONE egress-scheduler profile this NVUE has
# (default-global), so they apply switch-wide; there is no per-port binding to
# scope them with. That is harmless for every arm that does not steer anything
# into TC3 - a DWRR weight only decides anything when both queues have frames,
# and nothing sets a DSCP unless an experiment asks perftest for --tclass=106.
#
# The elapsed time this prints is itself a measurement: it is what a cloud
# operator pays to change a class weight with switch queues, the with-TC arm's
# answer to HyperFront's registry edit (evaluation_plan.md 2-4d). Report the
# whole `nv config apply`, not the `nv set`: the set is local bookkeeping and
# nothing on the wire changes until the apply lands.
#
# Buffer: run set_qos.sh apply tc alongside this so TC0 and TC3 each get their
# fixed 16 MB. Without it TC3 keeps whatever the standing single-queue mode
# left and the two classes are not comparable.
set -eu
case "${1:?status|set}" in
status)
  ssh -o BatchMode=yes sn5600 'nv show qos egress-scheduler default-global 2>/dev/null | grep -E "^ +[0-7] "' 2>&1 | grep -v Welcome ;;
set)
  R=${2:?rdma percent}
  [ "$R" -ge 0 ] && [ "$R" -le 100 ] || { echo "rdma percent must be 0..100"; exit 1; }
  T=$((100 - R))
  echo "TC3 (RDMA) = $R %, TC0 (TCP) = $T %"
  ssh -o BatchMode=yes sn5600 "
    nv set qos egress-scheduler default-global traffic-class 0 mode dwrr
    nv set qos egress-scheduler default-global traffic-class 0 bw-percent $T
    nv set qos egress-scheduler default-global traffic-class 3 mode dwrr
    nv set qos egress-scheduler default-global traffic-class 3 bw-percent $R" >/dev/null 2>&1
  S=$(date +%s.%N)
  ssh -o BatchMode=yes sn5600 'nv config apply -y' >/dev/null 2>&1
  E=$(date +%s.%N)
  printf "nv config apply took %.1f s\n" "$(echo "$E - $S" | bc)"
  ssh -o BatchMode=yes sn5600 'nv show qos egress-scheduler default-global 2>/dev/null | grep -E "^ +[03] "' 2>&1 | grep -v Welcome ;;
*) sed -n '2,20p' "$0"; exit 2 ;;
esac
