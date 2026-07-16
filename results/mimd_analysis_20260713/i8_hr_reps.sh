#!/usr/bin/env bash
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2; D=results/mimd_analysis_20260713
for hr in 0.03 0.10; do for rep in 1 2 3; do
  python3 -c "import json;r=json.load(open('$REPO/config/lab-registry.json'));r['e_params']['headroom']=$hr;json.dump(r,open('$REPO/config/lab-registry.json','w'),indent=2)"
  tag="i8r_${hr}_${rep}"; echo ">>> $tag"
  bash "$REPO/$D/incast8_run.sh" mimd zero 0.3 "$tag" 50 2>&1 | tail -1
done; done
python3 -c "import json;r=json.load(open('$REPO/config/lab-registry.json'));r['e_params']['headroom']=0.03;json.dump(r,open('$REPO/config/lab-registry.json','w'),indent=2)"
echo "HR-REPS-DONE"
