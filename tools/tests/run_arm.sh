#!/usr/bin/env bash
# run_arm.sh <tag> '<json e_params overrides>'  -> deploy, step run, analyze
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
TAG=$1; OVR=$2
python3 - "$OVR" <<'PY'
import json,sys
d=json.load(open('config/lab-registry.json')); d['e_params'].update(json.loads(sys.argv[1]))
json.dump(d,open('config/lab-registry.json','w'),indent=2); open('config/lab-registry.json','a').write("\n")
PY
bash tools/lab-infra/deploy_check.sh --deploy >/dev/null 2>&1 || { echo "deploy failed for $TAG"; bash tools/lab-infra/deploy_check.sh | tail -5; exit 1; }
bash tools/tests/step_regression.sh "$TAG" 2>&1 | tail -1
echo "## $TAG  $OVR" >> results/regression/step_arms.log
python3 tools/tests/analyze_step.py "$TAG" | tee -a results/regression/step_arms.log
