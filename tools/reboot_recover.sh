#!/usr/bin/env bash
# Restore the HPFT lab environment after a DPU reboot (e.g. a GBN<->SR switch).
#
# A DPU reboot loses RUNTIME state that is NOT persisted anywhere:
#   - VF netdevs (dpu1vf0-3) on both hosts;
#   - TCP EDT actuator (root fq + the pinned BPF egress prog) on the sender
#     -- lost when the VF netdev is recreated. Without it TCP is UNPACED,
#        blasts the link, and the resulting congestion crushes RDMA (which
#        obeys ECN) -- this looked like "SR crushes RDMA 0.19" until fixed;
#   - the telemetry underlay IP (10.1.9.x on underlay-p1) -- without it the
#     tx_agent gets no telemetry, the RP fail-opens, and RDMA runs to line;
#   - /tmp scripts (rp_service.sh).
# The vport-meter, pace-shim and rate-exporter are systemd units and survive.
#
# Run on the SENDER host (sgpu01) as a sudo-capable user, AFTER both DPUs are
# back up. Idempotent. The per-experiment runners still restart rx/tx agents
# and the RP themselves.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2
VFS="dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3"

echo "== 1/6 VFs on both hosts =="
bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1 || echo "  (sender vf_setup warn)"
ssh sgpu02 'bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1'
sleep 4
for d in $VFS; do sudo ip link set "$d" mtu 1500 2>/dev/null; done
ssh sgpu02 'for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu 1500 2>/dev/null; done'

echo "== 2/6 TCP EDT (fq + BPF) on sender =="
sudo bash "$REPO/tools/host/edt_ensure.sh"

echo "== 3/6 telemetry underlay-p1 (10.1.9.x) + static ARP =="
ssh hpft-dpu  'sudo ip addr add 10.1.9.1/24 dev underlay-p1 2>/dev/null; sudo ip link set underlay-p1 up'
ssh hpft-dpu2 'sudo ip addr add 10.1.9.2/24 dev underlay-p1 2>/dev/null; sudo ip link set underlay-p1 up'
M1=$(ssh hpft-dpu  'cat /sys/class/net/underlay-p1/address' 2>/dev/null)
M2=$(ssh hpft-dpu2 'cat /sys/class/net/underlay-p1/address' 2>/dev/null)
[ -n "$M2" ] && ssh hpft-dpu  "sudo arp -s 10.1.9.2 $M2 2>/dev/null"
[ -n "$M1" ] && ssh hpft-dpu2 "sudo arp -s 10.1.9.1 $M1 2>/dev/null"

echo "== 4/6 rp_service.sh + p1 100G + PFC off =="
scp -q "$REPO/tools/dpu/rp_service.sh" hpft-dpu:/tmp/rp_service.sh
ssh hpft-dpu2 'sudo ethtool -s p1 speed 100000 duplex full 2>/dev/null; sudo mlnx_qos -i p1 --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1'

echo "== 5/6 restart pace shim (re-seed pair state after EDT re-apply) =="
sudo systemctl restart hpft-pace-shim; sleep 2

echo "== 6/6 sanity =="
echo -n "  EDT: "; sudo bash "$REPO/tools/host/edt_ensure.sh" 2>/dev/null | tr '\n' ' '; echo
echo -n "  telemetry ping 10.1.9.2: "; ssh hpft-dpu 'ping -c3 -W1 10.1.9.2 2>&1 | tail -1'
echo "  vport_meter=$(ssh hpft-dpu2 'systemctl is-active hpft-vport-meter' 2>/dev/null) pace_shim=$(systemctl is-active hpft-pace-shim 2>/dev/null)"
echo "  retrans mode: $(ssh hpft-dpu 'sudo mlxconfig -d /dev/mst/mt41692_pciconf0 q RDMA_SELECTIVE_REPEAT_EN 2>/dev/null | grep -oE "True|False"')"
echo "== done -- runners (incast8_run.sh etc) will restart agents+RP =="
