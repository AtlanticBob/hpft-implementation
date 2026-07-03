#!/usr/bin/env bash
# Step 2 of 2: activate the NIC firmware written by the BFB (dpu card only).
# Tears down the 4 VFs on 38:00.1, fw-resets the card, recreates VFs and
# reapplies netplan. Run as root AFTER the new Arm OS is confirmed up.
set -euo pipefail
PF1=/sys/bus/pci/devices/0000:38:00.1

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "== current VF count: $(cat $PF1/sriov_numvfs)"
echo 0 > "$PF1/sriov_numvfs"
sleep 2

echo "== mlxfwreset 38:00.0 (both ports drop briefly; DPU Arm reboots) =="
mlxfwreset -d 38:00.0 --yes --sync 1 reset || {
    echo "sync reset failed; retrying default flow";
    mlxfwreset -d 38:00.0 --yes reset;
}
sleep 5

echo "== recreating 4 VFs =="
echo 4 > "$PF1/sriov_numvfs"
sleep 3
netplan apply || true
ip -br addr | grep -E "dpu1vf|dpu0|dpu1 " || true
echo "== firmware now: $(cat /sys/class/infiniband/mlx5_3/fw_ver) =="
echo "== done. If VF IPs are missing, re-add: ip addr add 10.1.<i>.1/24 dev dpu1vf<i>"
