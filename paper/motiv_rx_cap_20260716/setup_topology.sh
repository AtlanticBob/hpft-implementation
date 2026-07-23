#!/usr/bin/env bash
# Build the motivation-1.3 testbed from a clean/just-fw-reset state.
#
#   sender 4 VFs (each devlink tx_max=20G)  --VxLAN(VNI 100)-->  receiver vf0
#   receiver DPU decap point: OVS meter, 20 Gbps, drop band (naive cloud policer)
#
# Everything here is volatile: a fw reset wipes VFs, GUIDs, the OVS overlay,
# the meter, devlink rates and the ROCE_ACCL SR register. Re-run after any reset.
set -u

echo "== 1/6 VFs + GUIDs on both hosts (vf_setup.sh pins GUIDs) =="
bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1 || echo "  (sender vf_setup warn)"
ssh sgpu02 'bash /home/zhaoxiang/hyperfront/vf_setup.sh >/dev/null 2>&1'
sleep 4
for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu 1500 2>/dev/null; done
ssh sgpu02 'for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set "$d" mtu 1500 2>/dev/null; done'

echo "== 2/6 receiver vf0 takes all four source subnets =="
ssh sgpu02 'for i in 1 2 3; do sudo ip addr flush dev dpu1vf$i 2>/dev/null; done
for i in 1 2 3; do sudo ip addr add 10.1.$i.2/24 dev dpu1vf0 2>/dev/null; done
sudo ip link set dpu1vf0 up'

echo "== 3/6 VxLAN overlay (underlay IP goes DIRECTLY on p1; p1 must NOT be"
echo "        in a bridge, else decap offload fails and RoCE drops to ~1G) =="
setup_dpu() {
  local h=$1 lip=$2 rip=$3 vfs=$4
  ssh $h "sudo ovs-vsctl --if-exists del-port underlay-p1 p1
sudo ip addr flush dev underlay-p1 2>/dev/null
sudo ip addr flush dev p1 2>/dev/null
sudo ip addr add $lip/24 dev p1
sudo ip link set p1 up mtu 9000
sudo ovs-vsctl --if-exists del-br ovsbr-p1
sudo ovs-vsctl add-br ovsbr-p1
for i in $vfs; do sudo ovs-vsctl --if-exists del-port underlay-p1 pf1vf\$i; sudo ovs-vsctl --may-exist add-port ovsbr-p1 pf1vf\$i; done
sudo ovs-vsctl add-port ovsbr-p1 vxlan100 -- set interface vxlan100 type=vxlan options:local_ip=$lip options:remote_ip=$rip options:key=100 options:dst_port=4789
sudo ip link set ovsbr-p1 up"
}
setup_dpu hpft-dpu  172.16.1.1 172.16.1.2 "0 1 2 3"   # sender: all 4 VFs
setup_dpu hpft-dpu2 172.16.1.2 172.16.1.1 "0"         # receiver: vf0 only

echo "== 4/6 receiver 20G quota: class-blind OVS meter at the decap point =="
ssh hpft-dpu2 'sudo ovs-ofctl -O OpenFlow13 add-meter ovsbr-p1 "meter=1,kbps,band=type=drop,rate=20000000" 2>/dev/null
sudo ovs-ofctl -O OpenFlow13 add-flow ovsbr-p1 "in_port=vxlan100,priority=100,actions=meter:1,NORMAL" 2>/dev/null
sudo ovs-ofctl -O OpenFlow13 dump-meters ovsbr-p1 2>/dev/null | grep rate'

echo "== 5/6 sender: 20G tx_max per VF (offered 80G on a 200G link) =="
ssh hpft-dpu 'for i in 0 1 2 3; do sudo devlink port function rate set pci/0000:03:00.1/$((262145+i)) tx_max 20gbit; done
echo -n "  capped VFs: "; sudo devlink port function rate show | grep -c "tx_max 20Gbit"'
ssh hpft-dpu2 'sudo ethtool -s p1 speed 200000 duplex full 2>/dev/null; sleep 2; ethtool p1 2>/dev/null | grep Speed'

echo "== 6/6 flush stale ARP + verify the four overlay paths =="
sudo ip -s -s neigh flush all >/dev/null 2>&1
ssh sgpu02 'sudo ip -s -s neigh flush all >/dev/null 2>&1'
sleep 2
for v in 0 1 2 3; do
  printf "  dpu1vf%s -> 10.1.%s.2: " $v $v
  ping -c 3 -W 2 -q -I dpu1vf$v 10.1.$v.2 2>/dev/null | tail -1 | grep -oE "min/avg[^ ]*" || echo "UNREACHABLE"
done
echo "== retrans mode (set separately with cc_mode.sh sr|gbn) =="
sudo mlxreg -d 38:00.1 --reg_name ROCE_ACCL --get 2>/dev/null | grep "selective_repeat_forced_en "
echo -n "  LOG_TX_PSN_WINDOW: "; sudo mlxconfig -d 38:00.1 q LOG_TX_PSN_WINDOW 2>/dev/null | grep LOG_TX | awk '{print $2}'
