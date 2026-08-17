echo 0 | sudo tee /sys/bus/pci/devices/0000:38:00.1/sriov_numvfs
sleep 2
echo 4 | sudo tee /sys/bus/pci/devices/0000:38:00.1/sriov_numvfs
sleep 2

# VF node GUIDs: required for rdma_cm (zero-GUID VFs crash librdmacm and
# break CM connects). Host id: sgpu01=01/.1, sgpu02=02/.2. Set + driver rebind.
HOSTID=01; IPSUF=1
if [ "$(hostname)" = sgpu02 ]; then HOSTID=02; IPSUF=2; fi
for i in 0 1 2 3; do
    echo "00:11:22:33:${HOSTID}:00:00:0${i}" | sudo tee /sys/class/infiniband/mlx5_3/device/sriov/${i}/node >/dev/null
done
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do
    echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/unbind >/dev/null 2>&1
done
sleep 1
for p in 0000:38:02.3 0000:38:02.4 0000:38:02.5 0000:38:02.6; do
    echo $p | sudo tee /sys/bus/pci/drivers/mlx5_core/bind >/dev/null
done
sleep 3

for i in {0..3}; do
    sudo ip link set ens9f1v${i} down
    sudo ip link set ens9f1v${i} name dpu1vf${i}
    sudo ip addr flush dev dpu1vf${i}
    sudo ip addr add 10.1.${i}.${IPSUF}/24 dev dpu1vf${i}
    sudo ip link set dpu1vf${i} mtu 8192 up
done

# All four VFs share one L2 (the VxLAN overlay bridges every representor),
# so an ARP request for 10.1.i.<host> reaches every VF of that host and,
# with the kernel default arp_ignore=0, every VF answers with its OWN MAC;
# the peer keeps whichever reply lands last. TCP/ICMP still work (weak
# host model) but RoCE frames delivered to the wrong VF are dropped by the
# NIC (its GID table lacks that IP): "QP connects, zero iterations".
# arp_ignore=1: answer only for IPs configured on the receiving interface;
# arp_announce=2: source ARP from the interface that owns the address.
for d in all dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do
    sudo sysctl -q -w net.ipv4.conf.$d.arp_ignore=1 net.ipv4.conf.$d.arp_announce=2
done
sudo ip neigh flush to 10.1.0.0/16 2>/dev/null || true
