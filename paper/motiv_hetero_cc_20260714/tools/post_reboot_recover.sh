#!/usr/bin/env bash
# Motivation-1.2 post-DPU-reboot recovery: VFs, MTU 1500, 100G bottleneck,
# pause/PFC off (lossy), verify firmware CC flags. No E-stack, no EDT.
set -u
echo "== VFs on both hosts =="
bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1 || echo "(sender vf_setup warn)"
ssh sgpu02 'bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1'
sleep 4

echo "== VF node GUIDs (needed by rdma_cm; zero-GUID VFs crash librdmacm) =="
for i in 0 1 2 3; do echo "00:11:22:33:01:00:00:0$i" | sudo tee /sys/class/infiniband/mlx5_3/device/sriov/$i/node >/dev/null; done
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/unbind >/dev/null 2>&1; done
sleep 1
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/bind >/dev/null; done
sleep 4
for i in 0 1 2 3; do sudo ip link set ens9f1v${i} down 2>/dev/null; sudo ip link set ens9f1v${i} name dpu1vf${i} 2>/dev/null; sudo ip addr flush dev dpu1vf${i} 2>/dev/null; sudo ip addr add 10.1.${i}.1/24 dev dpu1vf${i} 2>/dev/null; sudo ip link set dpu1vf${i} up; done
ssh sgpu02 'for i in 0 1 2 3; do echo "00:11:22:33:02:00:00:0$i" | sudo tee /sys/class/infiniband/mlx5_3/device/sriov/$i/node >/dev/null; done
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/unbind >/dev/null 2>&1; done
sleep 1
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/bind >/dev/null; done
sleep 4
for i in 0 1 2 3; do sudo ip link set ens9f1v${i} down 2>/dev/null; sudo ip link set ens9f1v${i} name dpu1vf${i} 2>/dev/null; sudo ip addr flush dev dpu1vf${i} 2>/dev/null; sudo ip addr add 10.1.${i}.2/24 dev dpu1vf${i} 2>/dev/null; sudo ip link set dpu1vf${i} up; done'

for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu 1500 2>/dev/null; done
ssh sgpu02 'for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu 1500 2>/dev/null; done'
ibdev2netdev | grep dpu1vf
echo "guid check: $(cat /sys/class/infiniband/mlx5_6/node_guid) / $(ssh sgpu02 'cat /sys/class/infiniband/mlx5_6/node_guid')"

echo "== receiver bottleneck 100G =="
ssh hpft-dpu2 'sudo ethtool -s p1 speed 100000 duplex full 2>/dev/null; sleep 2; ethtool p1 | grep Speed'

echo "== lossy: pause off + PFC zero on both DPUs (p0 and p1) =="
ssh hpft-dpu  'for p in p0 p1; do sudo ethtool -A $p rx off tx off 2>/dev/null; sudo mlnx_qos -i $p --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1; done; ethtool -a p1 | tail -2'
ssh hpft-dpu2 'for p in p0 p1; do sudo ethtool -A $p rx off tx off 2>/dev/null; sudo mlnx_qos -i $p --pfc 0,0,0,0,0,0,0,0 >/dev/null 2>&1; done; ethtool -a p1 | tail -2'

echo "== firmware CC flags (current) =="
ssh hpft-dpu  'sudo mlxconfig -e -d /dev/mst/mt41692_pciconf0 q USER_PROGRAMMABLE_CC RDMA_SELECTIVE_REPEAT_EN 2>/dev/null | grep -E "USER_PROG|SELECTIVE"'
ssh hpft-dpu2 'sudo mlxconfig -e -d /dev/mst/mt41692_pciconf0 q USER_PROGRAMMABLE_CC RDMA_SELECTIVE_REPEAT_EN 2>/dev/null | grep -E "USER_PROG|SELECTIVE"'

echo "== no PCC / agents =="
ssh hpft-dpu 'pgrep -a -x doca_pcc; echo rc=$?'
ssh hpft-dpu2 'pgrep -af "rx_agent|doca_pcc"; echo rc=$?'

echo "== fabric ping =="
for v in 0 1 2 3; do ping -c 50 -i 0.05 -q 10.1.$v.2 | tail -1; done
