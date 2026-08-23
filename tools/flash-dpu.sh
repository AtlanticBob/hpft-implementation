#!/usr/bin/env bash
# Reflash this host's DPU card with the DOCA 3.4 BFB. Run as root.
# usage: flash-dpu.sh [PF bdf, e.g. 0000:b8:00.1]
# Step 1 of 2: locate the right rshim and push the BFB. The Arm reboots into
# the new OS at the end (~10-20 min). NIC firmware is written but only
# activates after activate-fw.sh (step 2).
set -euo pipefail
BFB=/home/zhaoxiang/hyperfront/bfb/bf-bundle-3.4.0-92_26.04_ubuntu-22.04_prod.bfb
CFG=/home/zhaoxiang/hyperfront/bfb/bf.cfg
# The card differs per machine (0000:38:00.x on sgpu01/02, 0000:b8:00.x on
# sgpu03/04), so the target is derived from this host's PF in the registry
# rather than written here. An explicit argument still wins.
PF=${1:-$(python3 -c "
import json, socket, sys
v=[x for x in json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json'))['vnics'] if x['host']==socket.gethostname()]
sys.exit('no vnics for this host in the tcp registry') if not v else print(v[0]['pf_bdf'])")}
[[ -n "$PF" ]] || exit 1
BASE=${PF%.*}          # 0000:38:00
TARGET_PCI=$BASE.2   # SoC management function of the dpu card

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -r $BFB && -r $CFG ]] || { echo "missing $BFB or $CFG"; exit 1; }

RSHIM=""
for r in /dev/rshim*; do
    [[ -d $r ]] || continue
    name=$(grep -aoE 'pcie-0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9]' "$r/misc" | head -1 || true)
    echo "$r -> ${name:-unknown}"
    [[ "$name" == "pcie-$TARGET_PCI" ]] && RSHIM=$(basename "$r")
done
[[ -n "$RSHIM" ]] || { echo "ERROR: no rshim maps to $TARGET_PCI - aborting"; exit 1; }

echo "== Flashing $BFB via $RSHIM (target $TARGET_PCI) =="
bfb-install --rshim "$RSHIM" --bfb "$BFB" --config "$CFG"
echo "== bfb-install finished. Wait for the Arm to boot, then verify:"
echo "   ssh hpft-dpu 'cat /etc/mlnx-release; dpkg -l | grep doca-runtime'"
