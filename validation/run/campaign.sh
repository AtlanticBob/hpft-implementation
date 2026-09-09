#!/usr/bin/env bash
# The whole validation suite, one scenario after another: V1 (DCQCN and
# Swift), V2, V4, V3, V5, V6, V7 on the standing single switch, then V8's
# two arms on the split switch (the loopback core is brought up for them
# and rolled back afterwards, so the standing state is restored). Every run
# is distilled, plotted and reported as it finishes.
#
#   campaign.sh <date-tag> [logfile]      tags are V<n>_bucket_<date-tag>
#
# Asking before running the full suite is the rule (validation/README.md
# section six); this script is what runs it once that has been agreed.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
DT=${1:?date tag, e.g. 20260909}
LOG=${2:-/tmp/campaign_$DT.log}
exec 9>/tmp/hpft_run.lock; until flock -n 9; do sleep 5; done; flock -u 9; exec 9>&-
one() { # $1 scenario, $2 tag, $3 fig base, rest: env assignments
  local scn=$1 tag=$2 base=$3; shift 3
  echo "=== $(date +%T) $scn $tag $*" >> "$LOG"
  env "$@" bash validation/run/run.sh "$scn" "$tag" >> "$LOG" 2>&1
  sleep 45   # the post-run switch snapshot lands 40 s after the run
  python3 validation/distill.py "$tag" >> "$LOG" 2>&1
  python3 validation/plot/timeline.py "$tag" "$base" >> "$LOG" 2>&1
  python3 validation/plot/executor.py "$tag" "$base" >> "$LOG" 2>&1
  python3 validation/report.py "$tag" >> "$LOG" 2>&1
  echo "=== $(date +%T) $tag finished" >> "$LOG"
}
one V1_incast             V1_bucket_$DT V1      X=1
one V1_incast             V1swift_$DT   V1swift HPFT_RDMA_CC_ALGO=3
one V2_flowset_join_leave V2_bucket_$DT V2      X=1
one V4_demand_change      V4_bucket_$DT V4      X=1
one V3_tenant_join_leave  V3_bucket_$DT V3      X=1
one V5_single_dst_cap     V5_bucket_$DT V5      X=1
one V6_other_cc           V6_bucket_$DT V6      X=1
one V7_cc_coexist         V7_bucket_$DT V7      X=1

# ---- V8 needs the split switch: bring the core up, run both arms, roll back ----
echo "=== $(date +%T) switch split: stage1" >> "$LOG"
bash tools/lab-infra/switch/split_stage1.sh >> "$LOG" 2>&1
sleep 15
bash tools/lab-infra/switch/split_verify.sh switch >> "$LOG" 2>&1
up=$(ssh -o BatchMode=yes sn5600 'nv show interface --view=brief 2>/dev/null' 2>/dev/null | awk '$1=="swp21"||$1=="swp25"{print $3}' | tr '\n' ' ')
echo "=== loop ports: $up" >> "$LOG"
if [ "$up" = "up up " ]; then
  trap 'bash tools/lab-infra/switch/split_rollback.sh >> "$LOG" 2>&1; echo "=== $(date +%T) switch rolled back" >> "$LOG"' EXIT
  bash tools/lab-infra/switch/split_cutover.sh >> "$LOG" 2>&1
  sleep 10
  bash tools/lab-infra/switch/split_verify.sh hosts >> "$LOG" 2>&1
  bash tools/lab-infra/switch/split_core_speed.sh status >> "$LOG" 2>&1
  one V8_core V8_bucket_$DT V8   X=1
  one V8_core V8cc_$DT      V8cc HPFT_CC_ONLY=1
else
  echo "=== V8 SKIPPED: loopback ports not up ($up)" >> "$LOG"
  bash tools/lab-infra/switch/split_rollback.sh >> "$LOG" 2>&1
fi
echo "=== $(date +%T) campaign done" >> "$LOG"
