#!/usr/bin/env bash
# Second pass of the 2026-09-08 campaign: V7 with a working UDP sink, then V8
# on the split switch (the loopback core is brought up for the two arms and
# rolled back afterwards, so the standing single-switch state is restored).
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
LOG=${1:-/tmp/campaign_20260908_b.log}
exec 9>/tmp/hpft_run.lock; until flock -n 9; do sleep 5; done; flock -u 9; exec 9>&-
one() { # $1 scenario, $2 tag, $3 fig base, rest: env assignments
  local scn=$1 tag=$2 base=$3; shift 3
  echo "=== $(date +%T) $scn $tag $*" >> "$LOG"
  env "$@" bash validation/run/run.sh "$scn" "$tag" >> "$LOG" 2>&1
  sleep 45
  python3 validation/distill.py "$tag" >> "$LOG" 2>&1
  python3 validation/plot/timeline.py "$tag" "$base" >> "$LOG" 2>&1
  python3 validation/plot/executor.py "$tag" "$base" >> "$LOG" 2>&1
  python3 validation/report.py "$tag" >> "$LOG" 2>&1
  echo "=== $(date +%T) $tag finished" >> "$LOG"
}
one V7_cc_coexist V7_bucket_20260908 V7 X=1

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
  one V8_core V8_bucket_20260908 V8 X=1
  one V8_core V8cc_20260908 V8cc HPFT_CC_ONLY=1
else
  echo "=== V8 SKIPPED: loopback ports not up ($up)" >> "$LOG"
  bash tools/lab-infra/switch/split_rollback.sh >> "$LOG" 2>&1
fi
echo "=== $(date +%T) campaign b done" >> "$LOG"
