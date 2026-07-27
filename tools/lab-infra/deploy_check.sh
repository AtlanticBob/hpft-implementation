#!/usr/bin/env bash
# Is what runs on the DPUs what is in the repo?
#
# Written after losing a lab run to the answer being no: tx_agent_e.py was
# deployed to the sender while the fastfill.py it had just started
# importing was not, so the sender agent crash-looped and RDMA ran
# completely unpaced. Every layer above looked healthy - the RP was up,
# the receiver was fine, the experiment produced a full set of numbers -
# and those numbers were meaningless. A crashed agent is not a silent
# failure mode anyone should have to notice by hand.
#
# usage: deploy_check.sh [--deploy]   (--deploy pushes what differs)
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
# The registry is included on purpose: runners push their own scenario
# copy, so a mismatch here means the lab is still carrying an experiment's
# configuration rather than the standing one. That is worth being told
# before the next experiment inherits it.
RX_FILES="tools/dpu/rx_agent.py tools/dpu/fastfill.py config/lab-registry.json"
TX_FILES="tools/dpu/tx_agent_e.py tools/dpu/fastfill.py config/lab-registry.json"
bad=0

cmp_one() { # cmp_one <host> <repo-path>
  local h=$1 f=$2 b
  b=$(basename "$f")
  local want have
  want=$(md5sum "$f" | cut -d' ' -f1)
  have=$(ssh "$h" "md5sum /opt/hpft/$b 2>/dev/null" | cut -d' ' -f1)
  if [ "$want" != "$have" ]; then
    echo "  DIFFERS  $h:/opt/hpft/$b"
    return 1
  fi
  return 0
}

for f in $RX_FILES; do cmp_one hpft-dpu2 "$f" || bad=1; done
for f in $TX_FILES; do cmp_one hpft-dpu  "$f" || bad=1; done

if [ "${1:-}" = "--deploy" ] && [ $bad -ne 0 ]; then
  echo "== deploying =="
  for f in $RX_FILES; do scp -q "$f" hpft-dpu2:/opt/hpft/; done
  for f in $TX_FILES; do scp -q "$f" hpft-dpu:/opt/hpft/; done
  # the shared object is per-architecture: rebuild on each Arm rather
  # than copying the x86 one that was built for the benchmark
  for h in hpft-dpu hpft-dpu2; do
    scp -q tools/dpu/fastfill.c "$h":/opt/hpft/
    ssh "$h" 'cd /opt/hpft && gcc -O2 -shared -fPIC -o libfastfill.so fastfill.c -lm' \
      || echo "  WARN $h: libfastfill.so build failed (agent falls back to Python)"
  done
  bad=0
  for f in $RX_FILES; do cmp_one hpft-dpu2 "$f" || bad=1; done
  for f in $TX_FILES; do cmp_one hpft-dpu  "$f" || bad=1; done
fi

# an agent that is not running is the same class of problem
for u in "hpft-dpu2 hpft-rxagent-e" "hpft-dpu hpft-txagent-e"; do
  set -- $u
  st=$(ssh "$1" "systemctl is-active $2" 2>/dev/null)
  [ "$st" = active ] || { echo "  NOT ACTIVE  $1/$2 ($st)"; bad=1; }
done
# ... and one that is up but crash-looping reports active only briefly.
# Scope this to the recent past, not the whole journal: a traceback from a
# fault already fixed is history, and a check that cries wolf about
# history is a check people learn to ignore.
for u in "hpft-dpu2 hpft-rxagent-e" "hpft-dpu hpft-txagent-e"; do
  set -- $u
  n=$(ssh "$1" "sudo journalctl -u $2 --no-pager -o cat --since '-90s' 2>/dev/null | grep -ac Traceback")
  [ "${n:-0}" -eq 0 ] || { echo "  TRACEBACKS  $1/$2: $n in the last 90 s"; bad=1; }
done

[ $bad -eq 0 ] && echo "deploy_check: repo == DPUs, both agents healthy" \
               || echo "deploy_check: MISMATCH (run with --deploy to push)"
exit $bad
