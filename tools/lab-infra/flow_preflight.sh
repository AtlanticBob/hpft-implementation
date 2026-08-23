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
#
# usage: flow_preflight.sh "0,0 1,1 2,2" [sender] [receiver]
#        -> exit 1 if any pair is dead. sender/receiver default to the
#        registry's, so the historical single-sender call still works; a
#        four-node run probes each sender in turn.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
PT=${PT:-$HOME/hyperfront/perftest-26015/ib_write_bw}
PAIRS=$1
SND=${2:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['sender_host'])")}
RCV=${3:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['receiver_host'])")}
PORT=${PORT:-27900}
# rdma device and IP of vf_n on a host: both differ per machine, and both are
# already written down once, in the registry.
dev_of() { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
ip_of()  { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
run_on() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -o BatchMode=yes "$h" "$*"; fi; }
fail=0
for ij in $PAIRS; do
  i=${ij%,*}; j=${ij#*,}
  run_on "$RCV" 'pkill -f "ib_write_[b]"; true' 2>/dev/null
  sleep 0.4
  ssh -o BatchMode=yes -f "$RCV" "nohup $PT -d $(dev_of $RCV $j) -p $PORT --report_gbits -D 3 >/dev/null 2>&1"
  sleep 1.2
  bw=$(run_on "$SND" "timeout 12 $PT -d $(dev_of $SND $i) -p $PORT --report_gbits -D 3 $(ip_of $RCV $j)" 2>&1 \
       | grep -A1 "BW average" | tail -1 | awk '{print $4}')
  # a pair that connects but moves nothing (0 iterations) is just as dead
  # as one that never connects: e.g. RoCE frames landing on the wrong VF
  # after a mis-learned ARP entry (TCP/ICMP fine, NIC drops the RoCE).
  if [ -z "$bw" ] || [ "$bw" = "BW" ] || awk "BEGIN{exit !($bw < 0.01)}"; then
    echo "  $SND/vf$i>$RCV/vf$j  DEAD (bw=${bw:-none})"; fail=1
  else
    echo "  $SND/vf$i>$RCV/vf$j  ok ${bw}G"
  fi
done
run_on "$RCV" 'pkill -f "ib_write_[b]"; true' 2>/dev/null
[ $fail -eq 0 ] && echo "preflight: all pairs live" || echo "preflight: FAILURES above"
exit $fail
