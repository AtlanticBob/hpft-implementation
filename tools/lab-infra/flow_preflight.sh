#!/usr/bin/env bash
# Verify every RDMA pair an experiment is about to use can actually move
# data, BEFORE the experiment runs.
#
# Why this exists: an RC error completion is terminal - a QP killed during
# a run never comes back - and every layer keeps reporting healthy while
# the wire stays at zero. Two ways to kill one are known (ops_notes,
# 2026-07-27): aggregate startup overload, and a large-ratio rate step.
# Both produced silent, wrong experiment data: one incast8 run read
# Jain=0.698 and was nearly reported as a fairness regression when the
# control plane had in fact allocated perfectly.
#
# Probes each pair briefly and IN SEQUENCE (never concurrently - the point
# is to test the pair, not to reproduce the overload).
# usage: flow_preflight.sh "0,0 1,1 2,2"   -> exit 1 if any pair is dead
set -u
PT=${PT:-$HOME/hyperfront/perftest-26015/ib_write_bw}
PAIRS=$1
PORT=${PORT:-27900}
fail=0
for ij in $PAIRS; do
  i=${ij%,*}; j=${ij#*,}
  ssh sgpu02 'pkill -f "ib_write_[b]"; true' 2>/dev/null
  sleep 0.4
  ssh -f sgpu02 "nohup $PT -d mlx5_$((6+j)) -p $PORT --report_gbits -D 3 >/dev/null 2>&1"
  sleep 1.2
  bw=$(timeout 12 $PT -d mlx5_$((6+i)) -p $PORT --report_gbits -D 3 10.1.$j.2 2>&1 \
       | grep -A1 "BW average" | tail -1 | awk '{print $4}')
  if [ -z "$bw" ] || [ "$bw" = "BW" ]; then
    echo "  vf$i>vf$j  DEAD"; fail=1
  else
    echo "  vf$i>vf$j  ok ${bw}G"
  fi
done
ssh sgpu02 'pkill -f "ib_write_[b]"; true' 2>/dev/null
[ $fail -eq 0 ] && echo "preflight: all pairs live" || echo "preflight: FAILURES above"
exit $fail
