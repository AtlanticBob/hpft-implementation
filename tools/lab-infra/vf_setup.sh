#!/usr/bin/env bash
# Rebuild this host's VFs and give them the identities the registry says.
#
# Registry-driven since the lab became four machines: the PF differs per host
# (0000:38:00.1 on sgpu01/02, 0000:b8:00.1 on sgpu03/04) and so do the VF
# functions and the kernel's names for them. All of it is already written down
# in config/lab-tcp-registry.json (pf_bdf, vf_pci_bdf) and lab-registry.json
# (ip, netdev), so none of it is written down a second time here. Run on the
# host as a sudo-capable user; idempotent.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
ME=$(hostname)

read -r PF NVF <<<"$(python3 - "$REPO" "$ME" <<'PY'
import json, sys
repo, me = sys.argv[1], sys.argv[2]
t = json.load(open(f"{repo}/config/lab-tcp-registry.json"))
v = [x for x in t["vnics"] if x["host"] == me]
if not v:
    sys.exit(f"vf_setup: {me} has no vnics in lab-tcp-registry.json")
print(v[0]["pf_bdf"], len(v))
PY
)" || exit 1

echo 0 | sudo tee /sys/bus/pci/devices/$PF/sriov_numvfs >/dev/null; sleep 2
echo $NVF | sudo tee /sys/bus/pci/devices/$PF/sriov_numvfs >/dev/null; sleep 3

# The PF's own rdma device is where the VF node GUIDs live. Find it from the
# PF address rather than assuming an mlx5_N: the index depends on how many
# other cards the host has and in what order they probed.
PFDEV=$(for d in /sys/class/infiniband/*; do
          [ "$(basename "$(readlink -f "$d/device")")" = "$PF" ] && basename "$d" && break
        done)
[ -n "${PFDEV:-}" ] || { echo "vf_setup: no rdma device for PF $PF"; exit 1; }

# VF node GUIDs: required for rdma_cm (zero-GUID VFs crash librdmacm and
# break CM connects). Host id is the digits of the hostname.
HOSTID=$(printf "%02d" "$(echo "$ME" | tr -dc 0-9 | sed 's/^0*//')")
for i in $(seq 0 $((NVF-1))); do
    echo "00:11:22:33:${HOSTID}:00:00:0${i}" \
      | sudo tee /sys/class/infiniband/$PFDEV/device/sriov/${i}/node >/dev/null
done

VFPCI=$(python3 - "$REPO" "$ME" <<'PY'
import json, sys
repo, me = sys.argv[1], sys.argv[2]
t = json.load(open(f"{repo}/config/lab-tcp-registry.json"))
print(" ".join(x["vf_pci_bdf"] for x in sorted(
    (x for x in t["vnics"] if x["host"] == me), key=lambda x: x["vf_index"])))
PY
)
for p in $VFPCI; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/unbind >/dev/null 2>&1; done
sleep 1
for p in $VFPCI; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/bind >/dev/null 2>&1; done
sleep 3

# Rename by PCI address, not by the name the kernel happened to pick: it is
# ens9f1vN on one host and ens31f1vN on another, and neither is a fact worth
# encoding here.
i=0
for p in $VFPCI; do
    cur=$(ls /sys/bus/pci/devices/$p/net 2>/dev/null | head -1)
    want=dpu1vf$i
    ip=$(python3 - "$REPO" "$ME" "$i" <<'PY'
import json, sys
repo, me, idx = sys.argv[1], sys.argv[2], sys.argv[3]
r = json.load(open(f"{repo}/config/lab-registry.json"))
print(next(v["ip"] for v in r["vnics"]
           if v["host"] == me and v["netdev"] == "dpu1vf" + idx))
PY
)
    if [ -n "$cur" ] && [ "$cur" != "$want" ]; then
        sudo ip link set "$cur" down
        sudo ip link set "$cur" name "$want"
    fi
    sudo ip addr flush dev "$want" 2>/dev/null
    sudo ip addr add "$ip/24" dev "$want"
    sudo ip link set "$want" mtu 8192 up
    i=$((i+1))
done

# All VFs share one L2 (the overlay bridges every representor), so an ARP
# request for 10.1.i.<host> reaches every VF of that host and, with the kernel
# default arp_ignore=0, every VF answers with its OWN MAC; the peer keeps
# whichever reply lands last. TCP/ICMP still work (weak host model) but RoCE
# frames delivered to the wrong VF are dropped by the NIC (its GID table lacks
# that IP): "QP connects, zero iterations".
for d in all $(seq -f "dpu1vf%g" 0 $((NVF-1))); do
    sudo sysctl -q -w net.ipv4.conf.$d.arp_ignore=1 net.ipv4.conf.$d.arp_announce=2
done
sudo ip neigh flush to 10.1.0.0/16 2>/dev/null || true
ip -br addr show | grep dpu1vf
