#!/usr/bin/env bash
# Is T = 1 ms justified, or is it just the value it has always had?
#
# Under v2 every constant is wall-clock and the law discretises on the
# measured interval, so T should be nearly free to raise:
#   - the law's trajectory is T-invariant: n steps of dt decay the gap by
#     (1-alpha)^n = exp(-k*dt*n) = exp(-k*t), which depends only on wall
#     clock;
#   - V is a wall-clock integral and does not scale with T by design;
#   - N1/N2, the discount band and the executor's 13 ms coalescing are
#     all wall-clock too.
# What T does buy is per-tick budget: the control loop's work is O(N) per
# tick regardless of T, so a larger T is a linear increase in flow-set
# capacity for no code at all.
#
# What it should cost is staleness margin. tau_eff ~ T + telemetry RTT +
# half the measurement window + executor coalescing, and the tuning rule
# is k*tau_eff <~ 0.4. Raising T eats into that budget, and what the
# budget protects is the JOINT climb of many flows on stale targets - so
# the failure to look for is multi-flow transient overshoot, not
# single-flow convergence (k is unchanged, so single-flow settling should
# be flat across the sweep - that arm is the control).
#
# Three things measured per T:
#   convergence   single-flow up-step, law-only (expect FLAT)
#   noise         steady-state sd of r (fewer samples per measurement
#                 window at larger T - the one thing that could get worse)
#   fairness      incast8 Jain and aggregate (expect unchanged)
# plus the receiver's tick histogram, which is where the capacity gain
# should show up directly.
#
# usage: sweep_run.sh [T_ms ...]     default: 1 2 4 8
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"; cd "$REPO"
TS=${*:-1 2 4 8}

set_period() {
  python3 - "$1" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
r["e_params"]["period_ms"] = int(sys.argv[1])
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
}

restore() { set_period 1; bash tools/lab-infra/deploy_check.sh --deploy >/dev/null 2>&1; }
trap restore EXIT

for T in $TS; do
  echo "===== T = ${T} ms ====="
  set_period "$T"
  bash tools/lab-infra/deploy_check.sh --deploy >/dev/null 2>&1
  ssh hpft-dpu2 'sudo systemctl restart hpft-rxagent-e' 2>/dev/null; sleep 3
  ssh hpft-dpu  'sudo systemctl restart hpft-txagent-e' 2>/dev/null; sleep 3
  # let the histogram fill on an idle loop, then capture it
  sleep 22
  ssh hpft-dpu2 "sudo journalctl -u hpft-rxagent-e --no-pager -o cat --since '-20s' | grep -a tick_us | tail -1" \
      > "$OUT/T${T}_tick.txt"
  bash "$REPO/results/acceptance_20260727/conv_step_v2.sh" "T${T}_step" >/dev/null 2>&1
  set_period "$T"   # conv_step's trap restored the repo copy; re-apply
  bash tools/lab-infra/deploy_check.sh --deploy >/dev/null 2>&1
  bash "$REPO/results/acceptance_20260727/incast8_v2.sh" "T${T}_i8"   >/dev/null 2>&1
  for f in step i8; do
    for s in rx tx t0; do
      ext=jsonl; [ "$s" = t0 ] && ext=txt
      src="$REPO/results/acceptance_20260727/results/T${T}_${f}_${s}.${ext}"
      [ -f "$src" ] && mv "$src" "$OUT/" 2>/dev/null
    done
  done
  echo "  T=${T}ms done"
done
echo "sweep-done"
