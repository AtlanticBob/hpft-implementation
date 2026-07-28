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
# Code mismatch is FATAL, configuration mismatch is a WARNING. Every
# runner pushes its own scenario registry, so a strict registry check
# makes chained runs impossible - and the failure it would be guarding
# against is not the same kind: the wrong registry produces a
# well-defined experiment with the wrong parameters, which the run's own
# output shows, whereas the wrong code produces a crashed agent and
# plausible numbers that show nothing.
RX_FILES="tools/dpu/rx_agent.py tools/dpu/fastfill.py"
TX_FILES="tools/dpu/tx_agent_e.py tools/dpu/fastfill.py"
bad=0
warn=0

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
cmp_one hpft-dpu2 config/lab-registry.json || warn=1
cmp_one hpft-dpu  config/lab-registry.json || warn=1

if [ "${1:-}" = "--deploy" ] && { [ $bad -ne 0 ] || [ $warn -ne 0 ]; }; then
  echo "== deploying =="
  for f in $RX_FILES; do scp -q "$f" hpft-dpu2:/opt/hpft/; done
  for f in $TX_FILES; do scp -q "$f" hpft-dpu:/opt/hpft/; done
  # the registry too. It is compared separately (mismatch is a warning,
  # not a fault) but it must still be PUSHED here, or --deploy silently
  # leaves the lab on the last experiment's scenario config - which is
  # the exact drift this tool exists to end.
  scp -q config/lab-registry.json hpft-dpu2:/opt/hpft/
  scp -q config/lab-registry.json hpft-dpu:/opt/hpft/
  # the shared object is per-architecture: rebuild on each Arm rather
  # than copying the x86 one that was built for the benchmark
  for h in hpft-dpu hpft-dpu2; do
    scp -q tools/dpu/fastfill.c "$h":/opt/hpft/
    ssh "$h" 'cd /opt/hpft && gcc -O2 -shared -fPIC -o libfastfill.so fastfill.c -lm' \
      || echo "  WARN $h: libfastfill.so build failed (agent falls back to Python)"
  done
  bad=0; warn=0
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

if [ $bad -eq 0 ]; then
  if [ $warn -ne 0 ]; then
    echo "deploy_check: code OK, agents healthy; registry differs (a"
    echo "              previous run's scenario config is still loaded)"
  else
    echo "deploy_check: repo == DPUs, both agents healthy"
  fi
else
  echo "deploy_check: CODE MISMATCH (run with --deploy to push)"
fi
exit $bad
