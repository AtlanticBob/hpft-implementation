#!/usr/bin/env bash
# One pass over the suite for the bucket executor (2026-09-08): waits for the
# run lock, then runs each scenario, distills it, draws its figures and writes
# its report. Every run's outcome goes to the campaign log.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
LOG=${1:-/tmp/campaign_20260908.log}
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
one V1_incast            V1_bucket_20260908 V1 X=1
one V1_incast            V1swift_20260908   V1swift HPFT_RDMA_CC_ALGO=3
one V2_flowset_join_leave V2_bucket_20260908 V2 X=1
one V4_demand_change     V4_bucket_20260908 V4 X=1
one V3_tenant_join_leave V3_bucket_20260908 V3 X=1
one V5_single_dst_cap    V5_bucket_20260908 V5 X=1
one V6_other_cc          V6_bucket_20260908 V6 X=1
one V8_core              V8_bucket_20260908 V8 X=1
one V8_core              V8cc_20260908      V8cc HPFT_CC_ONLY=1
one V7_cc_coexist        V7_bucket_20260908 V7 X=1
echo "=== $(date +%T) campaign done" >> "$LOG"
