#!/usr/bin/env bash
# Checks for the split (README.md).
#   split_verify.sh switch   after plugging: link, speed, STP state, BPDU counters, MAC table
#   split_verify.sh hosts    after cutover: jumbo ping across the core, one TCP pair, one RDMA pair
set -u
case "${1:-switch}" in
switch)
  ssh -o BatchMode=yes sn5600 '
    echo "== loop ports"; nv show interface --view=brief | grep -E "^swp2[15]\b"
    for p in swp21 swp25; do
      echo "== $p"
      nv show interface $p link 2>/dev/null | grep -iE "^\s*(speed|state|mtu|fec)"
      nv show interface $p bridge domain br_default stp 2>/dev/null | grep -iE "state|bpdu-filter|role"
      nv show interface $p counters 2>/dev/null | grep -iE "bpdu|error|discard" | head -4
    done
    echo "== bridge topology changes (should not grow after the plug)"
    nv show bridge domain br_default stp | grep -iE "count|change port"
    echo "== MACs learned per vlan (hosts of the other side should appear on the loop port)"
    nv show bridge domain br_default mac-table 2>/dev/null | grep -E "swp2[15]|swp37s|swp3s1|swp4s1" | head -20' 2>&1 | grep -v Welcome
  ;;
hosts)
  # The VxLAN underlay runs DPU to DPU on p1 (172.16.1.<n>, MTU 9000), so the
  # jumbo test is between DPUs; the VFs are MTU 1500 by rule. Both pairs cross
  # the core: dpu(sgpu01, A) -> dpu2(sgpu02, B), dpu3(A) -> dpu4(B). The TCP
  # and RDMA pairs use the registry's vf0 addresses and RDMA devices.
  echo "== underlay jumbo across the core (9000-byte frames, must not fragment)"
  ssh -n -o BatchMode=yes hpft-dpu  'ping -c 3 -W 1 -M do -s 8972 172.16.1.2 | tail -2' </dev/null
  ssh -n -o BatchMode=yes hpft-dpu3 'ping -c 3 -W 1 -M do -s 8972 172.16.1.4 | tail -1' </dev/null
  echo "== one TCP pair across the core, 5 s (expect ~47 G: the VF's 50 G cap, not the core)"
  ssh -n -o BatchMode=yes sgpu02 "setsid nohup iperf3 -s -p 5701 -1 >/tmp/split_s.log 2>&1 </dev/null &" </dev/null; sleep 2
  iperf3 -B 10.1.0.1%dpu1vf0 -c 10.1.0.2 -p 5701 -P 4 -t 5 -J 2>/dev/null | python3 -c "
import json,sys; j=json.load(sys.stdin)
print('ERROR', j['error']) if 'error' in j else print('tcp', round(j['end']['sum_received']['bits_per_second']/1e9,2), 'G, retrans', j['end']['sum_sent']['retransmits'], '(retransmits come from the VF policer; a same-side pair shows as many)')"
  echo "== one RDMA pair across the core, 5 s (expect ~45.6 G)"
  PT=$HOME/hyperfront/perftest-enhanced/ib_write_bw
  ssh -n -o BatchMode=yes sgpu02 "setsid nohup $PT -d mlx5_6 -q 4 -m 1024 -p 27500 --report_gbits -D 5 >/tmp/split_rs.log 2>&1 </dev/null &" </dev/null; sleep 2
  $PT -d mlx5_6 -q 4 -m 1024 -p 27500 --report_gbits -D 5 10.1.0.2 2>/dev/null | grep -E "^ *[0-9]+ +[0-9]+ " | tail -1
  ;;
esac
