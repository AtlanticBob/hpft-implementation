#!/usr/bin/env bash
# Reflash sgpu01's dpu card (PCI 0000:38:00.*) with DOCA 3.4 BFB. Run as root.
# Step 1 of 2: locate the right rshim and push the BFB. The Arm reboots into
# the new OS at the end (~10-20 min). NIC firmware is written but only
# activates after activate-fw.sh (step 2).
set -euo pipefail
BFB=/home/zhaoxiang/hyperfront/bfb/bf-bundle-3.4.0-92_26.04_ubuntu-22.04_prod.bfb
CFG=/home/zhaoxiang/hyperfront/bfb/bf.cfg
TARGET_PCI=0000:38:00.2   # SoC management function of the dpu card (NOT bf0/bf1)

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
