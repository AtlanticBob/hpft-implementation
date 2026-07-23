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
