#!/usr/bin/env bash
# Walk 2: does the DCQCN recovery aggressiveness decide whether Jakiro's
# weighted fairness converges? Fixed Jakiro (20G, 1:1), GBN. For each knob
# setting, restart Jakiro (fresh meter state) + fresh QPs, run 32 flows for
# 25s, record the RDMA-class steady rate. Repeat REP times to see which
# basin (fair ~10G vs runaway 28-46G) it lands in.
#   usage: probe_dcqcn_sweep.sh <knob> <rep_tag>
set -u
KNOB=$1; TAG=$2
BASE=/home/zhaoxiang/hyperfront/hpft-v2/paper/motiv_rx_cap_20260716
DIR=$BASE/dcqcn_sweep/${KNOB}_${TAG}
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RFN=/sys/class/net/dpu1/ecn/roce_rp
DUR=25
mkdir -p "$DIR"

# set DCQCN recovery knob (host PF sysfs)
case "$KNOB" in
  slow)    T=1200; A=1;  H=10  ;;
  default) T=300;  A=5;  H=50  ;;
  fast)    T=75;   A=50; H=500 ;;
esac
echo $T | sudo tee $RFN/rpg_time_reset >/dev/null
echo $A | sudo tee $RFN/rpg_ai_rate    >/dev/null
echo $H | sudo tee $RFN/rpg_hai_rate   >/dev/null

# restart Jakiro (fresh meter state)
ssh hpft-dpu2 'cd /home/ubuntu/bzx/jakiro_dhtb; sudo pkill -x jakiro_dhtb 2>/dev/null; sleep 2
sudo env JAKIRO_REP_PCI=0000:38:02.3 CAPACITY_GBPS=20 ROCE_WEIGHT_PERMILLE=500 UNDERLAY_SRC_IP=172.16.1.1 UNDERLAY_DST_IP=172.16.1.2 OVERLAY_DST_IP=10.1.0.2 VXLAN_VNI=100 ROCE_UDP_DST_PORT=4791 nohup ./build/jakiro_dhtb -- -a pci/0000:03:00.1 -l 50 > run/jakiro_dhtb.log 2>&1 &' >/dev/null 2>&1
sleep 12

ssh sgpu02 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'
pkill -f "ib_write_b[w]" 2>/dev/null || true
sleep 1
ssh sgpu02 'for p in $(seq 5301 5316); do nohup iperf3 -s -p $p >/dev/null 2>&1 & done; true'
ssh sgpu02 "for v in 0 1 2 3; do for i in 0 1 2 3; do
  nohup $PT -d mlx5_6 -p \$((19000+v*4+i)) --report_gbits -D $DUR > /tmp/js_s\${v}_\${i}.log 2>&1 &
done; done; true"
sleep 3

for v in 0 1 2 3; do for i in 0 1 2 3; do
  $PT -d mlx5_$((6+v)) -p $((19000+v*4+i)) --report_gbits -D $DUR 10.1.0.2 > "$DIR/rdma_v${v}_f${i}.log" 2>&1 &
done; done
for v in 0 1 2 3; do for j in 0 1 2 3; do
  iperf3 -c 10.1.0.2 -p $((5301+v*4+j)) -B 10.1.0.$((11+v)) -t $DUR -J > "$DIR/tcp_v${v}_f${j}.json" 2>&1 &
done; done
wait

python3 - "$DIR" "$KNOB" "$TAG" <<'PYEOF'
import re,json,glob,sys
D,knob,tag=sys.argv[1:4]
r=sum(float(re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)",open(f,errors='replace').read(),re.M).group(2) or 0)
      for f in glob.glob(f"{D}/rdma_v*_f*.log") if re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)",open(f,errors='replace').read(),re.M))
t=0.0
for f in glob.glob(f"{D}/tcp_v*_f*.json"):
    try: t+=json.load(open(f))['end']['sum_received']['bits_per_second']/1e9
    except: pass
basin="FAIR" if r<15 else "RUNAWAY"
print(f"  {knob}/{tag}: RDMA={r:.1f}G TCP={t:.1f}G  -> {basin}")
PYEOF
