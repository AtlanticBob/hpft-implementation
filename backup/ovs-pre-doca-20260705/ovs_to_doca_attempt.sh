#!/bin/bash
set -x
P0=0000:03:00.0; P1=0000:03:00.1
# 1. tear down existing kernel bridges
sudo ovs-vsctl --if-exists del-br underlay-p0
sudo ovs-vsctl --if-exists del-br underlay-p1
# 2. enable DOCA datapath + allow representors on both PFs
sudo ovs-vsctl --no-wait set Open_vSwitch . other_config:doca-init=true
sudo ovs-vsctl --no-wait set Open_vSwitch . other_config:hw-offload=true
sudo ovs-vsctl --no-wait set Open_vSwitch . other_config:pmd-cpu-mask=0xF000
sudo ovs-vsctl --no-wait set Open_vSwitch . other_config:dpdk-extra="-a $P0,representor=[pf0,sf0,vf0-3] -a $P1,representor=[pf1,sf0,vf0-3]"
# 3. restart OVS to init DPDK/DOCA EAL
sudo systemctl restart openvswitch-switch
sleep 6
# 4. recreate bridges (netdev datapath) + dpdk ports
sudo ovs-vsctl add-br underlay-p0 -- set bridge underlay-p0 datapath_type=netdev
sudo ovs-vsctl add-port underlay-p0 p0        -- set Interface p0        type=dpdk options:dpdk-devargs=$P0
sudo ovs-vsctl add-port underlay-p0 pf0hpf    -- set Interface pf0hpf    type=dpdk options:dpdk-devargs=$P0,representor=[65535]
sudo ovs-vsctl add-port underlay-p0 en3f0pf0sf0 -- set Interface en3f0pf0sf0 type=dpdk options:dpdk-devargs=$P0,representor=sf0
sudo ovs-vsctl add-br underlay-p1 -- set bridge underlay-p1 datapath_type=netdev
sudo ovs-vsctl add-port underlay-p1 p1        -- set Interface p1        type=dpdk options:dpdk-devargs=$P1
sudo ovs-vsctl add-port underlay-p1 pf1hpf    -- set Interface pf1hpf    type=dpdk options:dpdk-devargs=$P1,representor=[65535]
for i in 0 1 2 3; do sudo ovs-vsctl add-port underlay-p1 pf1vf$i -- set Interface pf1vf$i type=dpdk options:dpdk-devargs=$P1,representor=vf$i; done
sudo ovs-vsctl add-port underlay-p1 en3f1pf1sf0 -- set Interface en3f1pf1sf0 type=dpdk options:dpdk-devargs=$P1,representor=sf0
sleep 2
echo "=== result ==="; sudo ovs-vsctl show 2>&1 | head -40
echo "=== interface errors? ==="; sudo ovs-vsctl -f table --columns=name,error list interface 2>/dev/null | grep -v "\[\]" | head
