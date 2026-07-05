#!/bin/bash
set -x
# clear DOCA config
sudo ovs-vsctl --if-exists del-br underlay-p0
sudo ovs-vsctl --if-exists del-br underlay-p1
sudo ovs-vsctl --no-wait remove Open_vSwitch . other_config doca-init 2>/dev/null
sudo ovs-vsctl --no-wait remove Open_vSwitch . other_config dpdk-extra 2>/dev/null
sudo ovs-vsctl --no-wait remove Open_vSwitch . other_config dpdk-init 2>/dev/null
# stop everything hard
sudo systemctl stop openvswitch-switch 2>/dev/null
sudo pkill -9 ovs-vswitchd 2>/dev/null; sudo pkill -9 ovsdb-server 2>/dev/null; sleep 2
# restore pre-doca DB
sudo cp /tmp/conf.db.bak /var/lib/openvswitch/conf.db
sudo systemctl start openvswitch-switch
sleep 5
echo "=== other_config (should have NO doca-init) ==="; sudo ovs-vsctl get Open_vSwitch . other_config
echo "=== bridges ==="; sudo ovs-vsctl show 2>&1 | grep -E "Bridge|Port |datapath|error" | head -30
