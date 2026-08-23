#!/usr/bin/env bash
# Step 2 of 2: activate the NIC firmware written by the BFB (dpu card only).
# Tears down this host's VFs, fw-resets the card, recreates VFs and
# usage: activate-fw.sh [PF bdf, e.g. 0000:b8:00.1]
# reapplies netplan. Run as root AFTER the new Arm OS is confirmed up.
set -euo pipefail
# The card differs per machine (0000:38:00.x on sgpu01/02, 0000:b8:00.x on
# sgpu03/04), so the target is derived from this host's PF in the registry
# rather than written here. An explicit argument still wins.
PF=${1:-$(python3 -c "
import json, socket, sys
v=[x for x in json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json'))['vnics'] if x['host']==socket.gethostname()]
sys.exit('no vnics for this host in the tcp registry') if not v else print(v[0]['pf_bdf'])")}
[[ -n "$PF" ]] || exit 1
BASE=${PF%.*}          # 0000:38:00
PF1=/sys/bus/pci/devices/$PF

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "== current VF count: $(cat $PF1/sriov_numvfs)"
echo 0 > "$PF1/sriov_numvfs"
sleep 2

echo "== mlxfwreset ${BASE#0000:}.0 (both ports drop briefly; DPU Arm reboots) =="
mlxfwreset -d ${BASE#0000:}.0 --yes --sync 1 reset || {
    echo "sync reset failed; retrying default flow";
    mlxfwreset -d ${BASE#0000:}.0 --yes reset;
}
sleep 5

echo "== recreating 4 VFs =="
echo 4 > "$PF1/sriov_numvfs"
sleep 3
netplan apply || true
ip -br addr | grep -E "dpu1vf|dpu0|dpu1 " || true
echo "== firmware now: $(cat /sys/class/infiniband/mlx5_3/fw_ver) =="
echo "== done. If VF IPs are missing, re-add: ip addr add 10.1.<i>.1/24 dev dpu1vf<i>"
